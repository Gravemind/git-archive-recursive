#!/usr/bin/env python3

import os
import sys
from pathlib import Path
import subprocess
import argparse
from collections import OrderedDict
import argparse
import time
import multiprocessing

DEFAULT_NPROC = multiprocessing.cpu_count()

def make_parser():
    parser = argparse.ArgumentParser(
        description="Git archive with submodules, recursively, at any revision.",
        epilog=r"""

To support archiving any revision, git-archive-recursive looks up several
git-dirs until it finds the versioned submodules commits:

- looks into the corresponding, currently init, submodule's gitdir

- looks into the other currently init submodule's gitdir - **handles
  moved/renamed submodules**

- looks into `--lookup <git-dir>` directories - **handles removed
  submodules/commit** if you specified your own clone's .git containing the
  commit


Current known limitations:

- Only supports tar archive format

- `tar xf` is fine, but some extractor (7-zip) will complain about duplicated
  PAX tar header extension in the outputted tar (each submodule will declare
  it's `pax_global_header`).

- Does not automatically fetch/clone missing submodule/commit (you must use
  `--lookup`)

""",
        formatter_class=argparse.RawTextHelpFormatter)

    ga = parser.add_argument_group('git-archive(-like) arguments')

    ga.add_argument('tree-ish', nargs='?',
                    help='archive this commit, defaults to HEAD. See man git-archive')

    ga.add_argument('-o', '--output', metavar='<file>', type=str, required=True,
                    help='required, output archive file path. See man git-archive')

    ga.add_argument('--format', metavar='<format>', type=str,
                    help='archive format (tar). See man git-archive')

    ga.add_argument('-p', '--prefix', metavar='<prefix>/', type=str, default="",
                    help='prepend <prefix>/ to each filename in the archive. See man git-archive')

    gar = parser.add_argument_group('git-archive-recursive specific arguments')

    gar.add_argument('-j', metavar='<nproc>', dest='nproc', type=int, default=DEFAULT_NPROC,
                     help=f'number of concurrent git-archive jobs, 0 means infinity, defaults to {DEFAULT_NPROC}')

    gar.add_argument('--lookup', metavar='<git-dir-ish>', action='append', type=str,
                     help=f'add more ".git" directories to lookup into for old submodule commits')

    gar.add_argument('-d', '--depth', type=int, default=-1,
                     help='max recursive submodule depth, 0 means only the top git, -1 means infinite depth (the default)')

    gar.add_argument('-n', '--dry-run', dest='dryrun', action='store_true',
                     help='just verify everything is there, but don\'t actually create the output archive')

    gar.add_argument('--debug', action='store_true',
                     help='verbose display of debug information')

    return parser

def fatal(message):
    print("fatal error: "+message, file=sys.stderr)
    raise RuntimeError(message)

def warning(message):
    print("warning: "+message, file=sys.stderr)

def error(message):
    print("error: "+message, file=sys.stderr)

def info(message):
    print(message)

def iterable(obj):
    try:
        iter(obj)
    except Exception:
        return False
    else:
        return True

class RETCODE:
    pass
class STDERR:
    pass
class STDOUT:
    pass

DEBUG = False

def run(*cmd, ret=None):
    """Run a command.

    ret can be one of, or a tuple of, `None`, `RETCODE`, `STDERR`, `STDOUT`. And
    the function will return the corresponding value, or the tuple of
    corresponding values.

    If RETCODE is not in ret, then a fatal error is raised if cmd exits with a
    non-zero status.

    """
    cmd = [ str(c) for c in cmd ]
    if DEBUG:
        print(f'debug: running: {" ".join(cmd)}', file=sys.stderr)
    ret_iterable = iterable(ret)
    ret = (ret,) if not ret_iterable else ret
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if STDOUT in ret else None,
        stderr=subprocess.PIPE if STDERR in ret else None,
        encoding='utf-8',
    )
    if RETCODE not in ret and proc.returncode != 0:
        fatal("command failed with exit {}: {!r}".format(proc.returncode, cmd))
    def get_return_value(r):
        if r is None:
            return None
        if r == STDOUT:
            return proc.stdout
        if r == STDERR:
            return proc.stderr
        if r == RETCODE:
            return proc.returncode
        raise ValueError("run() got an invalid 'ret' kw parameter value: {!r}".format(r))
    return tuple([get_return_value(r) for r in ret])[ slice(None) if ret_iterable else 0 ]

def git(*subcommand, **kwargs):
    """Run a git sub command.

    kwargs pwd, gitdir, will be translated to git -C, git --git-dir.

    """
    cmd = ["git"]
    pwd = kwargs.pop("pwd", None)
    if pwd:
        cmd.extend(["-C", str(pwd)])
    gitdir = kwargs.pop("gitdir", None)
    if gitdir:
        assert not pwd, "gitdir and pwd conflicts !?"
        cmd.extend(["--git-dir", gitdir])
    cmd.extend(subcommand)
    return run(*cmd, **kwargs)

def is_valid_git_object(obj, typ, gitdir=None):
    """Return True if the obj of type typ is valid in gitdir"""
    retcode, stdout = git("rev-parse", "-q", "--verify", obj+"^{"+typ+"}",
                          gitdir=gitdir, ret=(RETCODE, STDOUT))
    assert retcode != 0 or stdout.strip() == obj, f"unexpected rev-parse {stdout!r} vs {obj!r}"
    return retcode == 0

def git_config(file=None, blob=None, gitdir=None):
    """Parse a git config file (or blob), and return it as a flat dict"""
    args = []
    if file:
        args.extend(["--file", file])
    if blob:
        assert not file, "cannot ask for blob and file"
        args.extend(["--blob", blob])
    stdout, retcode = git(
        "config",
        *args,
        "--list", # easier to parse
        gitdir=gitdir,
        ret=(STDOUT, RETCODE)
    )
    if retcode != 0:
        return None
    dic = OrderedDict()
    for line in stdout.splitlines():
        key, val = line.split('=', maxsplit=1)
        dic[key] = val
    return dic

def extract_submodule_config(configs, path):
    """Extract a submodule config given its path, from a git_config configs dict.

    Looks for a matching submodule path, not submodule name.
    """
    name = None
    for key, value in configs.items():
        if not key.startswith("submodule."):
            continue
        if not key.endswith(".path"):
            continue
        if value != path:
            continue
        name = key[len("submodule."):-len(".path")]
        break
    if name is None:
        return None
    config = OrderedDict()
    config['name'] = name
    dulekey = "submodule." + name + "."
    for key, value in configs.items():
        if key.startswith(dulekey):
            config[key[len(dulekey):]] = value
    return config

class GitDirCollection:
    """Collection of git-dirs where a rev/commit can be looked up into"""

    def __init__(self):
        self.gitdirs = dict()

    def add(self, path_hint, gitdir):
        """Add a git-dir to lookup into. path_hint is a hint for find later."""
        dirs = self.gitdirs.setdefault(str(path_hint), [])
        if gitdir not in dirs:
            dirs.append(Path(gitdir))

    def find(self, path_hint, rev):
        """Lookup for a git-dir containing rev, start with path_hint"""
        # try looking in the hinted path first
        gitdirs = self.gitdirs.get(str(path_hint), None)
        if gitdirs:
            for gitdir in gitdirs:
                if is_valid_git_object(rev, "commit", gitdir=gitdir):
                    return gitdir
        # else, look into all git-dirs.
        # (assumes there cannot be hash conflict between submodules)
        for gitdirs in self.gitdirs.values():
            for gitdir in gitdirs:
                if is_valid_git_object(rev, "commit", gitdir=gitdir):
                    return gitdir
        return None

def iter_git_submodules_at_rev(gitdir, rev):
    """Iterate over the submodules existing at rev.

    Yields tuples: (submodule-rel-path, submodule-rev)

    """
    # TODO: it would be less expensive if we could only output 'commit' objects in tree
    stdout = git("ls-tree", "-r", "--full-tree", rev,
                 gitdir=gitdir, ret=STDOUT)
    for line in stdout.splitlines():
        # <mode> SP <type> SP <object> TAB <file>
        chmod, typ, obj, path = line.split(maxsplit=3) # FIXME fail if path begins with a space!
        if typ != 'commit':
            continue
        yield (path, obj)

def recursively_iter_repos(top, top_gitdir, top_rev, gitdirs, depth_count_down=-1):
    yield (top, top_gitdir, top_rev)
    for path, rev in iter_git_submodules_at_rev(top_gitdir, top_rev):
        if depth_count_down == 0:
            info(f'Skipping depth {top}/{path} and below')
            continue

        assert path[0] != '/' and path[-1] != '/', "path format"
        fullpath = top + "/" + path
        gitdir = gitdirs.find(fullpath, rev)
        if gitdir is None:
            error(
                f"Could not find a git-dir for submodule {fullpath} commit {rev}:\n"
                f"  submodule commit: {rev}\n"
                f"  submodule path in parent: {path}\n"
                f"  parent path: {top}\n"
                f"  parent git-dir: {top_gitdir}\n"
                f"  parent commit: {top_rev}\n"
                f"(Maybe it just hasn't been `git submodule init` ?)"
            )
            configs = git_config(blob=top_rev + ":.gitmodules", gitdir=top_gitdir)
            if configs:
                config = extract_submodule_config(configs, path)
                if config:
                    cfg = "\n".join([ f"  {k}: {v}" for k, v in config.items() ])
                    info(f"FYI, the .gitmodule at that time described it like that:\n{cfg}");
            sys.exit(0)
        yield from recursively_iter_repos(fullpath, gitdir, rev, gitdirs,
                                          depth_count_down=depth_count_down - 1)

def iter_current_submodules_gitdirs(pwd=None):
    stdout = git("submodule", "foreach", "-q", "--recursive",
                 "echo $name $sm_path $toplevel $(git rev-parse --absolute-git-dir)", # FIXME spaces in paths ?
                 pwd=pwd,
                 ret=STDOUT)
    for line in stdout.splitlines():
        name, rel_path, parent, gitdir = line.split(' ', maxsplit=3)
        gitdir = Path(gitdir)
        assert parent[-1] != '/', "expect no trailing /"
        assert rel_path[0] != '/' "expect relative path, no leading /"
        abspath = Path(parent + '/' + rel_path)
        assert gitdir.is_dir(), "invalid submodule git dir {!r}".format(gitdir)
        assert gitdir.is_absolute(), "should have been absolute"
        yield (abspath, gitdir)

class ParallelJobs:
    """Handles asynchronously running commands"""

    def __init__(self, nproc):
        self.pending = []
        if nproc <= 0:
            nproc = -1
        self.nproc = nproc
        #print(f'running {self.nproc} parallel jobs')

    def launch(self, *cmd, userdata=None):
        if self.nproc > 1:
            while True:
                # TODO select ?
                actually_pending = sum([ p.poll() is None for p in self.pending ])
                if actually_pending < self.nproc:
                    break
                time.sleep(0.1)
        proc = subprocess.Popen(cmd)
        proc.userdata = userdata
        if self.nproc == 1:
            proc.wait() # wait here, easier to debug
        self.pending.append(proc)

    def can_wait(self):
        """Return True if there is still commands to wait"""
        return len(self.pending) > 0

    def wait_next_in_order(self):
        """Wait for the next command, in launch order, return its proc."""
        proc = self.pending.pop(0)
        ret = proc.wait()
        return proc

def main(args):
    parser = make_parser()

    opt = parser.parse_args(args)

    if opt.debug:
        global DEBUG
        DEBUG = True

    opt.rev = getattr(opt, 'tree-ish')
    if opt.rev is None:
        opt.rev = 'HEAD'

    if opt.format is None:
        for ext in ['tar.gz', 'tar', 'tgz', 'zip']:
            if opt.output.endswith('.' + ext):
                opt.format = ext

    SUPPORTED_FORMAT = ['tar']
    if opt.format not in SUPPORTED_FORMAT:
        fatal(f"unsupported format {opt.format!r}, {sys.argv[0]} only supports {SUPPORTED_FORMAT!r}")

    top_rev = git("rev-parse", opt.rev, ret=STDOUT).strip()

    gitdirs = GitDirCollection()

    top = git("rev-parse", "--show-toplevel", ret=STDOUT).strip()
    top_gitdir = git("rev-parse", "--absolute-git-dir", ret=STDOUT).strip()
    gitdirs.add(top, top_gitdir)

    for abspath, gitdir in iter_current_submodules_gitdirs(pwd=top):
        gitdirs.add(abspath, gitdir)

    if opt.lookup is not None:
        for lookup in opt.lookup:
            gitdirs.add('--lookup', lookup)

    jobs = ParallelJobs(opt.nproc)

    top_slash = top + '/'
    total = 0
    for fullpath, gitdir, rev in recursively_iter_repos(top, top_gitdir, top_rev, gitdirs, depth_count_down=opt.depth):
        prefix = fullpath + '/' # trailing / needed for --prefix
        assert prefix.startswith(top_slash), f"path {prefix!r} not under top {top_slash!r} ?"
        prefix = prefix[len(top_slash):]
        prefix = opt.prefix + prefix # now prepend root prefix option
        ud = argparse.Namespace()
        ud.step = 'gitarchive'
        # We could use rev for the filename, BUT buggee if same submodule+rev
        # commit at different location!
        ud.output = opt.output + "." + str(total)
        msg = f"git archive {total+1}: {prefix}"
        if DEBUG:
            msg += f"\n  gitdir:{str(gitdir)!r} at:{rev} output:{ud.output!r}"
        if opt.dryrun:
            info(f"Dry-run: {msg}")
        else:
            info(f"Launching {msg}")
            jobs.launch(
                "git", "--git-dir", gitdir, "archive",
                "--format", opt.format,
                "--output", ud.output, rev,
                "--prefix", prefix,
                userdata=ud)
        total += 1

    if opt.dryrun:
        info("Dry-run done.")
        return 0

    final_output = opt.output
    try:
        os.unlink(final_output) # tar will create it the first time
    except FileNotFoundError:
        pass

    done = 0
    while jobs.can_wait():
        info(f"Waiting and concatenating {done+1}/{total}...")
        proc = jobs.wait_next_in_order()
        if proc.returncode != 0:
            fatal(f"git archive command failed ({proc.returncode}): {proc.args!r}")
        ud = proc.userdata
        assert opt.format == 'tar', "only support tar"
        ret = run("tar", "--concatenate", "-f", final_output, ud.output)
        try:
            os.unlink(ud.output)
        except:
            warning(f"unexpected exception during cleanup of {ud.output!r}: {sys.exc_info()[0]}")
            pass
        done += 1

    info("Done.")

    return 0

if __name__ == "__main__":
    ret = main(sys.argv[1:])
    sys.exit(ret)

