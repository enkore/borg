#!/bin/sh -xue

T=testrepo
D=testdir

rm -rf $D $T
mkdir $D
borg init $T

echo "asdf" > $D/file1
echo "fdas" > $D/file2
echo "1234" > $D/file3
cp /boot/vmlinuz-linux $D

borg create $T::arch1 $D

echo "malicious code" >> $D/file3

borg create $T::arch2 $D

truncate -s 0 $D/file1

borg create $T::arch3 $D

borg diff $T::arch1 arch2
borg diff $T::arch1 arch3

