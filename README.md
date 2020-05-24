
# Git Recursive Archive

**Work In Progress!**

Git archive with submodules, recursively, at any revision.

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

```bash
/path/to/git-archive-recursive --help
```

```txt
usage: git-archive-recursive [-h] -o <file> [--format <format>] [-p <prefix>/] [-j <nproc>] [--lookup <git-dir-ish>] [-d DEPTH] [-n] [--debug] [tree-ish]

Git archive with submodules, recursively, at any revision.

optional arguments:
  -h, --help            show this help message and exit

git-archive(-like) arguments:
  tree-ish              archive this commit, defaults to HEAD. See man git-archive
  -o <file>, --output <file>
                        required, output archive file path. See man git-archive
  --format <format>     archive format (tar). See man git-archive
  -p <prefix>/, --prefix <prefix>/
                        prepend <prefix>/ to each filename in the archive. See man git-archive

git-archive-recursive specific arguments:
  -j <nproc>            number of concurrent git-archive jobs, 0 means infinity, defaults to 12
  --lookup <git-dir-ish>
                        add more ".git" directories to lookup into for old submodule commits
  -d DEPTH, --depth DEPTH
                        max recursive submodule depth, 0 means only the top git, -1 means infinite depth (the default)
  -n, --dry-run         just verify everything is there, but don't actually create the output archive
  --debug               verbose display of debug information
```

### Limitations

- Only supports tar archive format

- `tar xf` is fine, but some extractor (7-zip) will complain about duplicated
  PAX tar header extension in the outputted tar (each submodule will declare
  it's `pax_global_header`).


## Yet another?

I created this one because I wanted to archive old commit like the `git-archive`
command does (without checkout).

See other "git archive with submodules":

- https://github.com/rmiddle/git-archive-recursive
- https://github.com/Kentzo/git-archive-all
