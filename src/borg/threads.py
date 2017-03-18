import os
import resource
import signal
import socket
import stat
import struct
import sys
import threading
import traceback

import time
import zmq
from borg.item import Item, ArchiveItem
from datetime import timedelta, datetime

from getpass import getuser

from . import cache, xattr
from .archive import backup_io, is_special, Archive, Statistics, ChunkBuffer
from .chunker import Chunker
from .compress import Compressor
from .crypto import num_aes_blocks, blake2b_256, hmac_sha256
from .hashindex import unwrap_obj, wrap_obj
from .helpers import safe_encode, uid2user, gid2group, StableDict, make_path_safe, Manifest
from .logger import create_logger
from .platform import acl_get
from .platform import get_flags
from .platform import set_python_thread_affinity

log = create_logger(__name__)


class ThreadedService(threading.Thread):
    CONTROL_DIE = b'DIE'

    pure = True

    def __init__(self, zmq_context=None):
        super().__init__()
        self.context = zmq_context or zmq.Context().instance()
        self.running = True

    def control(self, opcode, *args):
        socket = self.context.socket(zmq.REQ)
        socket.connect(self._control_url)
        socket.send_multipart([opcode] + list(args))
        return socket.recv_multipart()

    def run(self):
        if self.pure:
            set_python_thread_affinity()
        try:
            t0 = time.monotonic()
            self.init()
            self.loop()
            td = time.monotonic() - t0
            ru = resource.getrusage(resource.RUSAGE_THREAD)
            rel = (ru.ru_utime + ru.ru_stime) / td * 100
            print(self.name, '%.2fs user, %.2fs sys, %.2fs wall, %d%%' % (ru.ru_utime, ru.ru_stime, td, rel))
        except Exception as e:
            print(file=sys.stderr)
            print('--- Critical error in thread', self.name, '---', file=sys.stderr)
            traceback.print_exc()
            print('--- Aborting application ---', file=sys.stderr)
            print(file=sys.stderr)
            os.kill(os.getpid(), signal.SIGABRT)

    def init(self):
        self.name = '%s-%x' % (self.__class__.__name__, self.ident)
        log.error(self.name)
        self.control_sock = self.context.socket(zmq.REP)
        self.control_sock.bind(self._control_url)
        self.poller = zmq.Poller()
        self.poller.register(self.control_sock, zmq.POLLIN)

    def loop(self):
        while self.running:
            events = dict(self.poller.poll())
            if self.control_sock in events:
                opcode, *args = self.control_sock.recv_multipart()
                self.handle_control(opcode, args)
                events.pop(self.control_sock)
            self.events(events)

    def handle_control(self, opcode, args):
        if opcode == self.CONTROL_DIE:
            self.running = False
        else:
            raise ValueError('Unknown ThreadedService opcode ' + repr(opcode))
        self.control_sock.send_multipart([b'ok'])

    def events(self, poll_events):
        pass

    @property
    def _control_url(self):
        assert self.ident is not None, 'service hasn\'t been started yet - can\'t control it'
        return 'inproc://thread-control/%s' % self.ident


class MetadataCollector:
    def __init__(self, noatime=False, noctime=False, numeric_owner=False):
        self.noatime = noatime
        self.noctime = noctime
        self.numeric_owner = numeric_owner

    def stat_attrs(self, st, path):
        """stat_simple_attrs + stat_ext_attrs"""
        attrs = self.stat_simple_attrs(st)
        attrs.update(self.stat_ext_attrs(st, path))
        return attrs

    def stat_simple_attrs(self, st):
        attrs = dict(
            mode=st.st_mode,
            uid=st.st_uid,
            gid=st.st_gid,
            mtime=st.st_mtime_ns,
        )
        # borg can work with archives only having mtime (older attic archives do not have
        # atime/ctime). it can be useful to omit atime/ctime, if they change without the
        # file content changing - e.g. to get better metadata deduplication.
        if not self.noatime:
            attrs['atime'] = st.st_atime_ns
        if not self.noctime:
            attrs['ctime'] = st.st_ctime_ns
        if self.numeric_owner:
            attrs['user'] = attrs['group'] = None
        else:
            attrs['user'] = uid2user(st.st_uid)
            attrs['group'] = gid2group(st.st_gid)
        return attrs

    def stat_ext_attrs(self, st, path):
        attrs = {}
        with backup_io('extended stat'):
            xattrs = xattr.get_all(path, follow_symlinks=False)
            bsdflags = get_flags(path, st)
            acl_get(path, attrs, st, self.numeric_owner)
        if xattrs:
            attrs['xattrs'] = StableDict(xattrs)
        if bsdflags:
            attrs['bsdflags'] = bsdflags
        return attrs


class FSOProcessors:
    """FS object processors"""

    def __init__(self, metadata_collector, add_item, process_file, zmq_context=None):
        self.metadata = metadata_collector
        self.add_item = add_item
        self.context = zmq_context or zmq.Context.instance()
        self._process_file = process_file
        self.num_items = 0

    def process_dir(self, path, st):
        item = Item(path=make_path_safe(path))
        item.status = 'd'
        item.update(self.metadata.stat_attrs(st, path))
        self.add_item(item)
        self.num_items += 1

    def process_fifo(self, path, st):
        item = Item(path=make_path_safe(path), status='f')
        item.update(self.metadata.stat_attrs(st, path))
        self.add_item(item)
        self.num_items += 1

    def process_dev(self, path, st):
        item = Item(path=make_path_safe(path), rdev=st.st_rdev)
        item.update(self.metadata.stat_attrs(st, path))
        if stat.S_ISCHR(st.st_mode):
            item.status = 'c'
        elif stat.S_ISBLK(st.st_mode):
            item.status = 'b'
        self.add_item(item)
        self.num_items += 1

    def process_symlink(self, path, st):
        with backup_io('readlink'):
            source = os.readlink(path)
        item = Item(path=make_path_safe(path), source=source)
        item.status = 's'
        item.update(self.metadata.stat_attrs(st, path))
        self.add_item(item)
        self.num_items += 1

    def process_stdin(self, path, cache):
        raise NotImplementedError

    def process_file(self, path, st, cache, ignore_inode=False):
        item = Item(path=make_path_safe(path))
        item.original_path = path
        self._process_file(item, path)
        self.num_items += 1


class FilesCacheService(ThreadedService):
    INPUT = 'inproc://files-cache'
    HARDLINK = 'inproc://files-cache/hardlink'
    CONTROL = 'inproc://files-cache/control'

    __doc__ = \
    """
    This is the first stage in the file processing pipeline.

    First, a file is checked against inodes seen so far to directly deduplicate
    hardlinks against each other ("h" status).

    Second, the file is checked against the files cache (the namesake of this),
    which also involves the chunks cache.

    If the files cache is hit, metadata is collected and the now finished item
    is transferred to the ItemBufferService.

    If the files cache is missed, processing continues to the ChunkerService.
    """

    @classmethod
    def get_process_file(cls):
        socket = zmq.Context.instance().socket(zmq.PUSH)
        socket.connect(FilesCacheService.INPUT)

        def process_file(item, path):
            socket.send_multipart([wrap_obj(item), path.encode()])

        return process_file

    def __init__(self, id_hash, metadata,
                 chunker_url=None, zmq_context=None):
        super().__init__(zmq_context)
        self.id_hash = id_hash
        self.metadata = metadata
        self.chunker_url = chunker_url or ChunkerService.INPUT
        self.cwd = safe_encode(os.getcwd())
        self.hard_links = {}

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.hardlink = self.context.socket(zmq.PULL)
        self.output = self.context.socket(zmq.PUSH)

        self.add_item = ItemBufferService.get_add_item()
        self.file_known_and_unchanged = ChunksCacheService.get_file_known_and_unchanged()

        self.poller.register(self.input)
        self.output.connect(self.chunker_url)
        self.input.bind(self.INPUT)
        self.hardlink.bind(self.HARDLINK)

    def events(self, poll_events):
        if self.input in poll_events:
            self.process_file()
        if self.hardlink in poll_events:
            self.add_hardlink_master()

    def process_file(self):
        item, path = self.input.recv_multipart()
        item = unwrap_obj(item)

        st = os.stat(path)

        # Is it a hard link?
        if st.st_nlink > 1:
            source = self.hard_links.get((st.st_ino, st.st_dev))
            if source is not None:
                item.source = source
                item.update(self.metadata.stat_attrs(st, path))
                item.status = 'h'  # regular file, hardlink (to already seen inodes)
                self.add_item(item)
                return

        # in --read-special mode, we may be called upon for special files.
        # there should be no information in the cache about special files processed in
        # read-special mode, but we better play safe as this was wrong in the past:
        is_special_file = is_special(st.st_mode)
        if not is_special_file:
            path_hash = self.id_hash(os.path.join(self.cwd, path))
            id_list = self.file_known_and_unchanged(path_hash, st)
            if id_list is not None:
                item.chunks = id_list
                item.status = 'U'

        # item is a hard link and has the chunks
        item.hardlink_master = st.st_nlink > 1
        item.update(self.metadata.stat_simple_attrs(st))

        if is_special_file:
            # we processed a special file like a regular file. reflect that in mode,
            # so it can be extracted / accessed in FUSE mount like a regular file:
            item.mode = stat.S_IFREG | stat.S_IMODE(item.mode)

        if 'chunks' not in item:
            # Only chunkify the file if needed
            item.status = 'A'
            item.chunks = []
            self.output.send_multipart([b'FILE' + wrap_obj(item), path])
        else:
            self.add_item(item)

    def add_hardlink_master(self):
        # Called by ItemHandler when a file is done and has the hardlink_master flag set
        stat_data, safe_path = self.input.recv_multipart()
        st_ino, st_dev = struct.unpack('=qq', stat_data)
        # Add the hard link reference *after* the file has been added to the archive.
        self.hard_links[st_ino, st_dev] = safe_path.decode()

    @classmethod
    def get_add_hardlink_master(cls):
        socket = zmq.Context.instance().socket(zmq.PUSH)
        socket.connect(cls.HARDLINK)

        def add_hardlink_master(item):
            stat_data = struct.pack('=qq', item.st_ino, item.st_dev)
            socket.send_multipart([stat_data, item.path.encode()])

        return add_hardlink_master


class ChunkerService(ThreadedService):
    # PULL: (ctx, path)
    INPUT = 'inproc://chunker'
    RELEASE_CHUNK = 'inproc://chunker/release-chunk'

    LARGE_CHUNK_TRESHOLD = 256 * 1024
    MEM_BUDGET = 64 * 1024 * 1024

    pure = False

    def __init__(self, chunker, zmq_context=None):
        super().__init__(zmq_context)
        self.chunker = chunker
        self.mem_budget = self.MEM_BUDGET

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.release_chunk = self.context.socket(zmq.PULL)

        self.output = self.context.socket(zmq.PUSH)
        self.finished_output = self.context.socket(zmq.PUSH)

        self.poller.register(self.input)
        self.poller.register(self.release_chunk)
        self.output.connect(IdHashService.INPUT)
        self.finished_output.connect(ItemHandler.FINISHED_INPUT)
        self.input.bind(self.INPUT)
        self.release_chunk.bind(self.RELEASE_CHUNK)

    def events(self, poll_events):
        if self.release_chunk in poll_events:
            self.mem_budget += int.from_bytes(self.release_chunk.recv(), sys.byteorder)
        if self.input in poll_events:
            self.chunk_file()

    def chunk_file(self):
        ctx, path = self.input.recv_multipart()
        n = 0
        fh = Archive._open_rb(path)
        with os.fdopen(fh, 'rb') as fd:
            for chunk in self.chunker.chunkify(fd, fh):
                # Important bit right here: The chunker gives us a memoryview of it's *internal* buffer,
                # so as soon as we return control back to it (via next() via iteration), it will start
                # copying new stuff into it's internal buffer. Therefore we need to make a copy here.

                if len(chunk) >= self.LARGE_CHUNK_TRESHOLD:
                    self.mem_budget -= len(chunk)
                    while self.mem_budget <= 0:
                        self.mem_budget += int.from_bytes(self.release_chunk.recv(), sys.byteorder)

                self.output.send_multipart([ctx, n.to_bytes(8, sys.byteorder), chunk], copy=True)
                n += 1
        self.finished_output.send_multipart([ctx, n.to_bytes(8, sys.byteorder)])


class IdHashService(ThreadedService):
    # PULL: (ctx, n, chunk)
    INPUT = 'inproc://id-hash'

    pure = False

    def __init__(self, id_hash, zmq_context=None):
        super().__init__(zmq_context)
        self.id_hash = id_hash

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.output = self.context.socket(zmq.PUSH)

        self.poller.register(self.input)
        self.output.connect(ChunksCacheService.INPUT)
        self.input.bind(self.INPUT)

    def events(self, poll_events):
        if self.input in poll_events:
            ctx, n, chunk = self.input.recv_multipart(copy=False)
            id = self.id_hash(chunk.buffer)
            self.output.send_multipart([ctx, n, chunk, id], copy=False)


class ChunksCacheService(ThreadedService):
    # PULL: (ctx, n, chunk, id)
    INPUT = 'inproc://cache'

    # PULL: (ctx, n, id, csize, size)
    CHUNK_SAVED = 'inproc://cache/chunk-saved'

    # REP: path_hash, packed_st -> (ChunkListEntry, ...) or (,) if file not known
    FILE_KNOWN = 'inproc://cache/file-known'

    MEMORIZE = 'inproc://cache/memorize-file'

    CONTROL_COMMIT = b'COMMIT'

    @classmethod
    def get_file_known_and_unchanged(cls):
        socket = zmq.Context.instance().socket(zmq.REQ)
        socket.connect(cls.FILE_KNOWN)

        def file_known_and_unchanged(path_hash, st):
            """
            *path_hash* is the id_hash of the file to be queried; st is a standard os.stat_result.

            Returns None, for a changed or unknown files, or a chunk ID list.
            """
            packed_st = struct.pack('=qqq', st.st_ino, st.st_size, st.st_mtime_ns)
            socket.send_multipart([path_hash, packed_st])
            response = socket.recv_multipart()
            if not response:
                return
            chunks_list = []
            for entry in response:
                # could/should use ChunkListEntry here?
                if entry == b'0':
                    return None
                if entry:
                    chunks_list.append(struct.unpack('=32sLL', entry))
            return chunks_list

        return file_known_and_unchanged

    class StatResult:
        st_mode = stat.S_IFREG
        st_ino = st_size = st_mtime_ns = 0

    def __init__(self, backend_cache: cache.Cache, zmq_context=None):
        super().__init__(zmq_context)
        self.cache = backend_cache
        self.stats = Statistics()
        self.st = self.StatResult()

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.chunk_saved = self.context.socket(zmq.PULL)
        self.memorize = self.context.socket(zmq.PULL)
        self.file_known = self.context.socket(zmq.REP)

        self.output_new = self.context.socket(zmq.PUSH)
        self.output_seen = self.context.socket(zmq.PUSH)
        self.output_release_chunk = self.context.socket(zmq.PUSH)

        self.poller.register(self.input)
        self.poller.register(self.chunk_saved)
        self.poller.register(self.memorize)
        self.poller.register(self.file_known)
        self.input.bind(self.INPUT)
        self.chunk_saved.bind(self.CHUNK_SAVED)
        self.memorize.bind(self.MEMORIZE)
        self.file_known.bind(self.FILE_KNOWN)
        self.output_new.connect(CompressionService.INPUT)
        self.output_seen.connect(ProcessedChunkRouter.INPUT)
        self.output_release_chunk.connect(ChunkerService.RELEASE_CHUNK)

    def events(self, poll_events):
        if self.input in poll_events:
            self.route_chunk()
        if self.chunk_saved in poll_events:
            self.add_new_saved_chunk()
        if self.memorize in poll_events:
            self.memorize_file()
        if self.file_known in poll_events:
            self.respond_file_known()
        self.stats.show_progress(dt=0.2)

    def handle_control(self, opcode, args):
        if opcode == self.CONTROL_COMMIT:
            self.cache.commit()
            self.control_sock.send(b'OK')
            return
        if opcode == self.CONTROL_DIE:
            print(self.stats)
        super().handle_control(opcode, args)

    def route_chunk(self):
        ctx, n, chunk, id = self.input.recv_multipart(copy=False)
        id = id.bytes
        if self.cache.seen_chunk(id):
            self.output_seen.send_multipart([ctx, n, struct.pack('=32sLL', *self.cache.chunk_incref(id, self.stats))])
            if len(chunk) >= ChunkerService.LARGE_CHUNK_TRESHOLD:
                self.output_release_chunk.send(len(chunk).to_bytes(4, sys.byteorder))
        else:
            self.output_new.send_multipart([ctx, n, chunk, id], copy=False)

    def add_new_saved_chunk(self):
        ctx, n, id, sizes = self.chunk_saved.recv_multipart()
        csize, size = struct.unpack('=LL', sizes)
        if size >= ChunkerService.LARGE_CHUNK_TRESHOLD:
            self.output_release_chunk.send(size.to_bytes(4, sys.byteorder))
        self.cache.chunks.chunks.add(id, 1, size, csize)
        # Depending on how long chunk processing takes we may issue the same chunk multiple times, so it will
        # be stored a few times and reported here a few times. This is unproblematic, since these are compacted
        # away by the Repository.
        #
        # This is the same issue that Thomas already observed in late 2015 in his first approach to multi-threading
        # and resulted in a separate "delayer thread" there. Here it isn't really a big issue, just an in-elegance.
        # If we avoid assigning meaningful value to csize in the ChunkIdLists, then we can get rid of this,
        # and directly push ChunkListEntries from the repository to the chunk entry router.
        refcount = self.cache.seen_chunk(id)
        self.stats.update(size, csize, refcount == 1)
        self.output_seen.send_multipart([ctx, n, struct.pack('=32sLL', id, size, csize)])

    def respond_file_known(self):
        path_hash, st = self.file_known.recv_multipart()
        self.st.st_ino, self.st.st_size, self.st.st_mtime_ns = struct.unpack('=qqq', st)

        ids = self.cache.file_known_and_unchanged(path_hash, self.st)

        if ids is None:
            self.file_known.send(b'0')
            return

        if not all(self.cache.seen_chunk(id) for id in ids):
            self.file_known.send(b'0')
            return

        chunk_list = [struct.pack('=32sLL', *self.cache.chunk_incref(id, self.stats)) for id in ids]
        if chunk_list:
            self.file_known.send_multipart(chunk_list)
        else:
            self.file_known.send(b'')

    def memorize_file(self):
        path_hash, st, *ids = self.memorize.recv_multipart()
        self.st.st_ino, self.st.st_size, self.st.st_mtime_ns = struct.unpack('=qqq', st)
        self.cache.memorize_file(path_hash, self.st, ids)


class CompressionService(ThreadedService):
    INPUT = 'inproc://compression'

    pure = False

    def __init__(self, zmq_context=None):
        super().__init__(zmq_context)
        self.compressor = Compressor('lz4')

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.output = self.context.socket(zmq.PUSH)

        self.poller.register(self.input)
        self.input.bind(self.INPUT)
        self.output.connect(EncryptionService.INPUT)

    def events(self, poll_events):
        if self.input in poll_events:
            ctx, n, chunk, id = self.input.recv_multipart(copy=False)
            size = len(chunk.buffer).to_bytes(4, sys.byteorder)
            compressed = self.compressor.compress(chunk.buffer)
            self.output.send_multipart([ctx, n, compressed, id, size], copy=False)


class EncryptionService(ThreadedService):
    INPUT = 'inproc://encryption'

    pure = False

    def __init__(self, key, zmq_context=None):
        super().__init__(zmq_context)
        self.key = key

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.output = self.context.socket(zmq.PUSH)

        self.poller.register(self.input)
        self.input.bind(self.INPUT)
        self.output.connect(RepositoryService.INPUT)

    def events(self, poll_events):
        if self.input in poll_events:
            ctx, n, chunk, id, size = self.input.recv_multipart(copy=False)

            # Copied mostly from AESKeyBase.encrypt, we'll need to update that API
            self.key.nonce_manager.ensure_reservation(num_aes_blocks(len(chunk.buffer)))
            self.key.enc_cipher.reset()
            data = b''.join((self.key.enc_cipher.iv[8:], self.key.enc_cipher.encrypt(chunk.buffer)))
            assert (self.key.MAC is blake2b_256 and len(self.key.enc_hmac_key) == 128 or
                    self.key.MAC is hmac_sha256 and len(self.key.enc_hmac_key) == 32)
            hmac = self.key.MAC(self.key.enc_hmac_key, data)
            encrypted = b''.join((self.key.TYPE_STR, hmac, data))

            self.output.send_multipart([ctx, n, encrypted, id, size], copy=False)


class RepositoryService(ThreadedService):
    INPUT = 'inproc://repository/put'
    API = 'inproc://repository'

    CONTROL_COMMIT = b'COMMIT'

    def __init__(self, repository, chunk_saved_url=ChunksCacheService.CHUNK_SAVED, zmq_context=None):
        super().__init__(zmq_context)
        self.repository = repository
        self.chunk_saved_url = chunk_saved_url

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.api = self.context.socket(zmq.REP)
        self.output = self.context.socket(zmq.PUSH)

        self.poller.register(self.input)
        self.poller.register(self.api)
        self.input.bind(self.INPUT)
        self.api.bind(self.API)
        self.output.connect(self.chunk_saved_url)

    def events(self, poll_events):
        if self.input in poll_events:
            self.put()
        if self.api in poll_events:
            self.api_reply()

    def handle_control(self, opcode, args):
        if opcode == self.CONTROL_COMMIT:
            self.repository.commit()
            self.control_sock.send(b'OK')
        else:
            super().handle_control(opcode, args)

    def put(self):
        ctx, n, data, id, size = self.input.recv_multipart()
        self.repository.put(id, data)
        self.output.send_multipart([ctx, n, id, len(data).to_bytes(4, sys.byteorder) + size])

    def api_reply(self):
        pass


class ProcessedChunkRouter(ThreadedService):
    INPUT = 'inproc://chunk-router'

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.item_output = self.context.socket(zmq.PUSH)
        self.meta_output = self.context.socket(zmq.PUSH)

        self.poller.register(self.input)
        self.input.bind(self.INPUT)
        self.item_output.connect(ItemHandler.CHUNK_INPUT)
        self.meta_output.connect(ItemBufferService.CHUNK_INPUT)

    def events(self, poll_events):
        if self.input in poll_events:
            ctx, n, chunk_list_entry = self.input.recv_multipart()
            if ctx.startswith(b'FILE'):
                self.item_output.send_multipart([ctx[4:], n, chunk_list_entry])
            elif ctx.startswith(b'META'):
                self.meta_output.send_multipart([ctx, n, chunk_list_entry])
            else:
                raise ValueError('Unknown context prefix: ' + repr(ctx[4:]))


class ItemHandler(ThreadedService):
    CHUNK_INPUT = 'inproc://item-handler/chunks'
    FINISHED_INPUT = 'inproc://item-handler/finished'

    def __init__(self, metadata_collector: MetadataCollector, zmq_context=None):
        super().__init__(zmq_context)
        self.metadata = metadata_collector

    def init(self):
        super().init()
        self.chunk_input = self.context.socket(zmq.PULL)
        self.finished_chunking_file = self.context.socket(zmq.PULL)

        self.items_in_progress = {}

        self.chunk_input.bind(self.CHUNK_INPUT)
        self.finished_chunking_file.bind(self.FINISHED_INPUT)

        self.poller.register(self.chunk_input)
        self.poller.register(self.finished_chunking_file)

        self.add_item = ItemBufferService.get_add_item()
        self.add_hardlink_master = FilesCacheService.get_add_hardlink_master()

    def events(self, poll_events):
        if self.chunk_input in poll_events:
            self.process_chunk()
        if self.finished_chunking_file in poll_events:
            self.set_item_finished()

    def process_chunk(self):
        ctx, chunk_index, chunk_list_entry = self.chunk_input.recv_multipart()
        if ctx not in self.items_in_progress:
            self.items_in_progress[ctx] = unwrap_obj(ctx)
        item = self.items_in_progress[ctx]
        chunk_index = int.from_bytes(chunk_index, sys.byteorder)
        if chunk_index >= len(item.chunks):
            item.chunks.extend([None] * (chunk_index - len(item.chunks) + 1))
        item.chunks[chunk_index] = struct.unpack('=32sLL', chunk_list_entry)
        self.check_item_done(ctx, item)

    def set_item_finished(self):
        ctx, num_chunks = self.finished_chunking_file.recv_multipart()
        assert ctx.startswith(b'FILE')
        ctx = ctx[4:]
        if ctx not in self.items_in_progress:
            self.items_in_progress[ctx] = unwrap_obj(ctx)
        item = self.items_in_progress[ctx]
        num_chunks = int.from_bytes(num_chunks, sys.byteorder)
        item.num_chunks = num_chunks
        self.check_item_done(ctx, item)

    def check_item_done(self, ctx, item):
        if getattr(item, 'num_chunks', None) == len(item.chunks) and all(item.chunks):
            del self.items_in_progress[ctx]

            st = os.stat(item.original_path)
            item.update(self.metadata.stat_attrs(st, item.original_path))
            if item.get('hardlink_master'):
                self.add_hardlink_master(item)

            self.add_item(item)

import enum


class ItemBufferService(ChunkBuffer, ThreadedService):
    CHUNK_INPUT = 'inproc://chunk-buffer/chunks'
    ITEM_INPUT = 'inproc://chunk-buffer/add-item'

    CONTROL_SAVE = b'SAVE'

    class States(enum.Enum):
        WAITING_FOR_SAVE = 0
        WAITING_FOR_ITEMS = 1
        WAITING_FOR_ARCHIVE_CHUNKS = 2
        WAITING_FOR_ARCHIVE_ITEM = 3
        WAITING_FOR_MANIFEST = 4

    ARCHIVE_ITEM_CTX = b'META_ARCHIVE_ITEM'
    MANIFEST_CTX = b'META_MANIFEST'

    @classmethod
    def get_add_item(cls):
        socket = zmq.Context.instance().socket(zmq.PUSH)
        socket.connect(cls.ITEM_INPUT)

        def add_item(item):
            socket.send(wrap_obj(item))

        return add_item

    def __init__(self, key, archive, cache_control, repository_control,
                 push_chunk_url=IdHashService.INPUT, compress_url=CompressionService.INPUT):
        super().__init__(key)
        self.archive = archive
        self.push_chunk_url = push_chunk_url
        self.compress_url = compress_url
        self.cache_control = cache_control
        self.repository_control = repository_control

        self.num_items = 0
        self.save_after_item_no = None
        self.die_after_save = False

        self.state = self.States.WAITING_FOR_SAVE

    def init(self):
        super().init()
        self.add_item = self.context.socket(zmq.PULL)
        self.chunk_added = self.context.socket(zmq.PULL)

        self.push_chunk = self.context.socket(zmq.PUSH)
        self.compress_chunk = self.context.socket(zmq.PUSH)

        self.poller.register(self.add_item)
        self.poller.register(self.chunk_added)
        self.add_item.bind(self.ITEM_INPUT)
        self.chunk_added.bind(self.CHUNK_INPUT)
        self.push_chunk.connect(self.push_chunk_url)
        self.compress_chunk.connect(self.compress_url)

    def events(self, poll_events):
        if self.add_item in poll_events:
            self.pack_and_add_item()
        if self.chunk_added in poll_events:
            ctx, n, chunk_list_entry = self.chunk_added.recv_multipart()
            chunk_id, *_ = struct.unpack('=32sLL', chunk_list_entry)
            if self.state == self.States.WAITING_FOR_ARCHIVE_ITEM and ctx == self.ARCHIVE_ITEM_CTX:
                self.update_manifest(chunk_id)
            elif self.state == self.States.WAITING_FOR_MANIFEST and ctx == self.MANIFEST_CTX:
                self.commit()
            else:
                n = int.from_bytes(n, sys.byteorder)
                self.chunks[n] = chunk_id
        if self.state == self.States.WAITING_FOR_ITEMS and self.num_items == self.save_after_item_no:
            # Got all items, flush (no-op if nothing buffered)
            self.flush(flush=True)
            self.state = self.States.WAITING_FOR_ARCHIVE_CHUNKS
        if self.state == self.States.WAITING_FOR_ARCHIVE_CHUNKS and self.is_complete():
            # If we're complete we can go ahead and make the archive item,
            # update the manifest and commit.
            self.save_archive()

    def handle_control(self, opcode, args):
        if opcode == self.CONTROL_SAVE:
            assert len(args) == 1
            assert self.state == self.States.WAITING_FOR_SAVE
            self.state = self.States.WAITING_FOR_ITEMS
            self.save_after_item_no = int.from_bytes(args[0], sys.byteorder)
            # Reply is sent _after_ it's done.
        else:
            super().handle_control(opcode, args)

    def save(self, fso_processors):
        num_items = fso_processors.num_items
        self.control(self.CONTROL_SAVE, num_items.to_bytes(8, sys.byteorder))

    def pack_and_add_item(self):
        item = unwrap_obj(self.add_item.recv())
        self.num_items += 1
        self.add(item)

    def write_chunk(self, chunk):
        ctx = b'META'
        n = len(self.chunks).to_bytes(8, sys.byteorder)
        self.push_chunk.send_multipart([ctx, n, chunk.data])
        return None  # we'll fill the ID later in

    def is_complete(self):
        return all(self.chunks)

    def save_archive(self, name=None, comment=None, timestamp=None, additional_metadata=None):
        # largely copied from Archive.save
        name = name or self.name
        if name in self.archive.manifest.archives:
            raise Archive.AlreadyExists(name)
        duration = timedelta(seconds=time.monotonic() - self.archive.start_monotonic)
        if timestamp is None:
            end = datetime.utcnow()
            start = end - duration
        else:
            end = timestamp
            start = timestamp - duration
        self.start = start
        metadata = {
            'version': 1,
            'name': name,
            'comment': comment or '',
            'items': self.chunks,
            'cmdline': sys.argv,
            'hostname': socket.gethostname(),
            'username': getuser(),
            'time': start.isoformat(),
            'time_end': end.isoformat(),
            'chunker_params': self.archive.chunker_params,
        }
        metadata.update(additional_metadata or {})
        metadata = ArchiveItem(metadata)
        data = self.key.pack_and_authenticate_metadata(metadata.as_dict(), context=b'archive')
        self.push_chunk.send_multipart([self.ARCHIVE_ITEM_CTX, b'', data])
        self.state = self.States.WAITING_FOR_ARCHIVE_ITEM

    def update_manifest(self, archive_item_id):
        self.archive.manifest.archives[self.archive.name] = (archive_item_id, self.start.isoformat())
        manifest_data = self.archive.manifest.get_data()
        self.archive.manifest.id = self.key.id_hash(manifest_data)
        self.compress_chunk.send_multipart([self.MANIFEST_CTX, b'', manifest_data, Manifest.MANIFEST_ID])
        self.state = self.States.WAITING_FOR_MANIFEST

    def commit(self):
        self.repository_control(RepositoryService.CONTROL_COMMIT)
        self.cache_control(ChunksCacheService.CONTROL_COMMIT)
        self.state = self.States.WAITING_FOR_SAVE
        # Notify main thread that we're done here.
        self.control_sock.send(b'OK')


# --- Ancillary services


class ErrorHandlingEngine(ThreadedService):
    INPUT = 'inproc://error-handling-engine/file-failed'

    def init(self):
        super().init()
        self.input = self.context.socket(zmq.PULL)
        self.input.bind(self.INPUT)
        self.poller.register(self.input)


# god's own
class CreationPipeline:
    def __init__(self, archive, key, cache):
        zmq.Context.instance(io_threads=0)
        self.metadata_collector = MetadataCollector()

        self.files_cache = FilesCacheService(key.id_hash, self.metadata_collector)
        chunker = Chunker(key.chunk_seed, *archive.chunker_params)
        self.chunker = ChunkerService(chunker)
        self.id_hash = IdHashService(key.id_hash)
        self.chunks_cache = ChunksCacheService(cache)
        self.compressor = CompressionService()
        self.encryption = EncryptionService(key)
        self.repository = RepositoryService(archive.repository)
        self.pcr = ProcessedChunkRouter()
        self.ih = ItemHandler(self.metadata_collector)
        self.cbs = ItemBufferService(key, archive,
                                     cache_control=self.chunks_cache.control,
                                     repository_control=self.repository.control,)

        self.fso = FSOProcessors(self.metadata_collector, self.cbs.get_add_item(), self.files_cache.get_process_file())

        self.services = [
            self.files_cache, self.chunker, self.id_hash,
            self.chunks_cache, self.compressor, self.encryption,
            self.repository, self.pcr, self.ih, self.cbs,
        ]

    def save(self):
        self.cbs.save(self.fso)

    def __enter__(self):
        for service in self.services:
            service.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            log.error('--- Critical error ---')
            traceback.print_exc()
            os.kill(os.getpid(), signal.SIGABRT)
        else:
            log.debug('Joining threads...')
            for service in self.services:
                log.debug('Terminating %s', service.__class__.__name__)
                service.control(ThreadedService.CONTROL_DIE)
                log.debug('Joining %s', service.__class__.__name__)
                service.join()
            log.debug('Joined all %d threads.', len(self.services))
