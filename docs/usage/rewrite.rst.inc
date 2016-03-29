.. _borg_rewrite:

borg rewrite
------------
::

    usage: borg rewrite [-h] [-v] [--debug] [--lock-wait N] [--show-version]
                        [--show-rc] [--no-files-cache] [--umask M]
                        [--remote-path PATH] [--list] [-p] [-f] [-n] [-s]
                        [-e PATTERN] [--exclude-from EXCLUDEFILE]
                        [--exclude-caches] [--exclude-if-present FILENAME]
                        [--keep-tag-files] [-C COMPRESSION]
                        [--chunker-params CHUNK_MIN_EXP,CHUNK_MAX_EXP,HASH_MASK_BITS,HASH_WINDOW_SIZE]
                        [REPOSITORY_OR_ARCHIVE] [PATH [PATH ...]]
    
    Rewrite archive contents
    
    positional arguments:
      REPOSITORY_OR_ARCHIVE
                            repository/archive to rewrite
      PATH                  paths to rewrite; patterns are supported
    
    optional arguments:
      -h, --help            show this help message and exit
      -v, --verbose, --info
                            enable informative (verbose) output, work on log level
                            INFO
      --debug               enable debug output, work on log level DEBUG
      --lock-wait N         wait for the lock, but max. N seconds (default: 1).
      --show-version        show/log the borg version
      --show-rc             show/log the return code (rc)
      --no-files-cache      do not load/update the file metadata cache used to
                            detect unchanged files
      --umask M             set umask to M (local and remote, default: 0077)
      --remote-path PATH    set remote path to executable (default: "borg")
      --list                output verbose list of items (files, dirs, ...)
      -p, --progress        show progress display while rewriting archives
      -f, --force           even recompress chunks already compressed with the
                            algorithm set with --compression
      -n, --dry-run         do not change anything
      -s, --stats           print statistics at end
      -e PATTERN, --exclude PATTERN
                            exclude paths matching PATTERN
      --exclude-from EXCLUDEFILE
                            read exclude patterns from EXCLUDEFILE, one per line
      --exclude-caches      exclude directories that contain a CACHEDIR.TAG file
                            (http://www.brynosaurus.com/cachedir/spec.html)
      --exclude-if-present FILENAME
                            exclude directories that contain the specified file
      --keep-tag-files      keep tag files of excluded caches/directories
      -C COMPRESSION, --compression COMPRESSION
                            select compression algorithm (and level): none == no
                            compression (default), lz4 == lz4, zlib == zlib
                            (default level 6), zlib,0 .. zlib,9 == zlib (with
                            level 0..9), lzma == lzma (default level 6), lzma,0 ..
                            lzma,9 == lzma (with level 0..9).
      --chunker-params CHUNK_MIN_EXP,CHUNK_MAX_EXP,HASH_MASK_BITS,HASH_WINDOW_SIZE
                            specify the chunker parameters (or "default").
    
Description
~~~~~~~~~~~

Rewrites the contents of existing archives.

--exclude, --exclude-from and PATH have the exact same semantics
as in borg create, this means if a PATH is specified the
rewrite includes that path and nothing else: PATH does *not* restrict
the rewrite to a path.

--compression recompresses all chunks. Due to how Borg stores compressed size
information this might display incorrect information for archives that were not
rewritten at the same time.
There is no risk of data loss by this. Use --force to recompress chunks already
using the specified compression algorithm.

--chunker-params will re-chunk all files in the archive, this can be
used to have upgraded Borg 0.xx or Attic archives deduplicate with
Borg 1.x archives.

Currently the only file status used for --list is 'I' (file/dir included in
rewritten archive).

borg rewrite is signal safe. Send either SIGINT (Ctrl-C on most terminals) or
SIGTERM to request termination.

Use the *exact same* command line to resume the operation later - changing excludes
or paths will lead to inconsistencies (changed excludes will only apply to newly
processed files/dirs). Changing compression leads to incorrect size information
(which does not cause any data loss, but can be misleading).

USE WITH CAUTION. Permanent data loss by specifying incorrect patterns is possible.

Note: The archive under rewrite is only removed after the operation completes. The
      archive that is built during the rewrite exists at the same time at
      <ARCHIVE>.rewrite.

Note: When recompressing or (especially) rechunking space usage can be substantial.

Note: This changes the archive ID.
