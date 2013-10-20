#
# Copyright (c) 2010-2013 Liraz Siri <liraz@turnkeylinux.org>
#
# This file is part of TKLBAM (TurnKey Linux BAckup and Migration).
#
# TKLBAM is open source software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of
# the License, or (at your option) any later version.
#

import os
from os.path import exists, join

import shutil
import utils
import simplejson

from paths import Paths

from dirindex import read_paths
from changes import whatchanged
from pkgman import Packages

import mysql
import pgsql

from utils import AttrDict

class ProfilePaths(Paths):
    files = [ 'dirindex', 'dirindex.conf', 'packages' ]

class ExtrasPaths(Paths):
    PATH = "TKLBAM"
    def __init__(self, backup_root=None):
        if backup_root is None:
            backup_root = '/'

        Paths.__init__(self, join(backup_root, self.PATH))

    def __new__(cls, root_path=None):
        return str.__new__(cls, root_path)

    files = [ 'backup-conf', 'fsdelta', 'fsdelta-olist', 'newpkgs', 'pgfs', 'myfs', 'etc', 'etc/mysql' ]

def _rmdir(path):
    if exists(path):
        shutil.rmtree(path)

def _fpaths(dpath):
    arr = []
    for dpath, dnames, fnames in os.walk(dpath):
        for fname in fnames:
            arr.append(join(dpath, fname))
    return arr

def _filter_deleted(files):
    return [ f for f in files if exists(f) ]

def print_if(conditional):
    def printer(s):
        if conditional:
            print s
    return printer

class BackupConf(AttrDict):
    def __init__(self, profile_id, overrides, skip_files, skip_packages, skip_database):
        AttrDict.__init__(self)
        self.profile_id = profile_id
        self.overrides = overrides
        self.skip_files = skip_files
        self.skip_packages = skip_packages
        self.skip_database = skip_database

    @classmethod
    def fromfile(cls, path):
        if not exists(path):
            return None

        d = simplejson.load(file(path))
        return cls(*(d[attr]
                     for attr in ('profile_id', 'overrides', 'skip_files', 'skip_packages', 'skip_database')))

    def tofile(self, path):
        simplejson.dump(dict(self), file(path, "w"))

class Backup:
    class Error(Exception):
        pass

    @staticmethod
    def _write_new_packages(dest, base_packages):
        base_packages = Packages.fromfile(base_packages)
        current_packages = Packages()

        fh = file(dest, "w")
        new_packages = list(current_packages - base_packages)
        new_packages.sort()
        for package in new_packages:
            print >> fh, package
        fh.close()

    @staticmethod
    def _write_whatchanged(dest, dest_olist, dirindex, dirindex_conf,
                           overrides=[]):
        paths = read_paths(file(dirindex_conf))
        paths += overrides

        changes = whatchanged(dirindex, paths)
        changes.sort(lambda a,b: cmp(a.path, b.path))
        olist = [ change.path for change in changes if change.OP == 'o' ]

        changes.tofile(dest)
        file(dest_olist, "w").writelines((path + "\n" for path in olist))

    @classmethod
    def _create_extras(cls, extras, profile, conf):
        os.mkdir(extras.path)
        os.chmod(extras.path, 0700)

        etc = str(extras.etc)
        os.mkdir(etc)
        shutil.copy("/etc/passwd", etc)
        shutil.copy("/etc/group", etc)

        if not conf.skip_files:
            cls._write_whatchanged(extras.fsdelta, extras.fsdelta_olist,
                                   profile.dirindex, profile.dirindex_conf,
                                   conf.overrides.fs)

        if not conf.skip_packages:
            cls._write_new_packages(extras.newpkgs, profile.packages)

        if not conf.skip_database:
            try:
                mysql.backup(extras.myfs, extras.etc.mysql,
                             limits=conf.overrides.mydb)
            except mysql.Error:
                pass

            try:
                pgsql.backup(extras.pgfs, conf.overrides.pgdb)
            except pgsql.Error:
                pass

    def __init__(self, profile, overrides, 
                 skip_files=False, skip_packages=False, skip_database=False, resume=False, verbose=True, extras_root="/"):

        log = print_if(verbose)

        if not profile:
            raise self.Error("can't backup without a profile")

        profile_paths = ProfilePaths(profile.path)
        extras_paths = ExtrasPaths(extras_root)

        # decide whether we can allow resume=True
        # /TKLBAM has to exist and the backup configuration has to match
        backup_conf = BackupConf(profile.profile_id,
                                 overrides,
                                 skip_files,
                                 skip_packages,
                                 skip_database)

        saved_backup_conf = BackupConf.fromfile(extras_paths.backup_conf)

        if backup_conf != saved_backup_conf:
            resume = False

        if not resume:
            _rmdir(extras_paths.path)
        else:
            log("ATTEMPTING TO RESUME ABORTED BACKUP SESSION")

        self.resume = resume

        # create or re-use /TKLBAM
        if not exists(extras_paths.path):
            log("CREATING " + extras_paths.path)

            try:
                self._create_extras(extras_paths, profile_paths, backup_conf)
                backup_conf.tofile(extras_paths.backup_conf)
            except:
                # destroy potentially incomplete extras
                _rmdir(extras_paths.path)
                raise

        # print uncompressed footprint
        if verbose:

            # files in /TKLBAM + /TKLBAM/fsdelta-olist
            fpaths= _fpaths(extras_paths.path)

            if not skip_files:
                fsdelta_olist = file(extras_paths.fsdelta_olist).read().splitlines()
                fpaths += _filter_deleted(fsdelta_olist)

            size = sum([ os.lstat(fpath).st_size
                         for fpath in fpaths ])

            if size > 1024 * 1024 * 1024:
                size_fmt = "%.2f GB" % (float(size) / (1024 * 1024 * 1024))
            elif size > 1024 * 1024:
                size_fmt = "%.2f MB" % (float(size) / (1024 * 1024))
            else:
                size_fmt = "%.2f KB" % (float(size) / 1024)

            log("FULL UNCOMPRESSED FOOTPRINT: %s in %d files" % (size_fmt, len(fpaths)))

        self.extras_paths = extras_paths

    def dump(self, path):
        def r(p):
            return join(path, p.lstrip('/'))

        if exists(self.extras_paths.fsdelta_olist):
            utils.apply_overlay('/', path, self.extras_paths.fsdelta_olist)
