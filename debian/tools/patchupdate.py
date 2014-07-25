#!/usr/bin/python
#
# Automatic patch dependency checker and Makefile/README.md generator.
#
# Copyright (C) 2014 Sebastian Lackner
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA
#

from multiprocessing import Pool
from xml.dom import minidom
import contextlib
import hashlib
import itertools
import os
import patchutils
import pickle
import re
import subprocess
import sys
import textwrap
import urllib

# Cached information to speed up patch dependency checks
cached_patch_result = {}

class PatchUpdaterError(RuntimeError):
    """Failed to update patches."""
    pass

class AuthorInfo(object):
    def __init__(self):
        self.author         = ""
        self.subject        = ""
        self.revision       = ""

class PatchSet(object):
    def __init__(self, name):
        self.name           = name
        self.authors        = []
        self.fixes          = []
        self.changes        = []

        self.files          = []
        self.patches        = []
        self.modified_files = set()
        self.depends        = set()

        self.verify_depends = set()
        self.verify_time    = None

def download(url):
    """Open a specific URL and return the content."""
    with contextlib.closing(urllib.urlopen(url)) as fp:
        return fp.read()

def read_patchsets(directory):
    """Read information about all patchsets in a given directory."""

    def _iter_kv_from_file(filename):
        with open(filename) as fp:
            for line in fp:
                if line.startswith("#"):
                    continue
                tmp = line.split(":", 1)
                if len(tmp) != 2:
                    yield None, None
                else:
                    yield tmp[0].lower(), tmp[1].strip()

    unique_id   = itertools.count()
    all_patches = {}
    name_to_id  = {}
    all_bugs    = []

    # Read in sorted order (to ensure created Makefile doesn't change too much)
    for name in sorted(os.listdir(directory)):
        if name in [".", ".."]: continue
        subdirectory = os.path.join(directory, name)
        if not os.path.isdir(subdirectory): continue

        patch = PatchSet(name)

        # Enumerate .patch files in the given directory, enumerate individual patches and affected files
        for f in sorted(os.listdir(subdirectory)):
            if not f.endswith(".patch") or not os.path.isfile(os.path.join(subdirectory, f)):
                continue
            patch.files.append(f)
            for p in patchutils.read_patch(os.path.join(subdirectory, f)):
                patch.patches.append(p)
                patch.modified_files.add(p.modified_file)

        # No single patch within this directory, ignore it
        if len(patch.patches) == 0:
            del patch
            continue

        i = next(unique_id)
        all_patches[i]   = patch
        name_to_id[name] = i

    # Now read the definition files in a second step
    for i, patch in all_patches.iteritems():
        deffile = os.path.join(os.path.join(directory, patch.name), "definition")

        if not os.path.isfile(deffile):
            raise PatchUpdaterError("Missing definition file %s" % deffile)

        info = AuthorInfo()

        for key, val in _iter_kv_from_file(deffile):
            if key is None:
                if len(info.author) and len(info.subject) and len(info.revision):
                    patch.authors.append(info)
                    info = AuthorInfo()
                continue

            if key == "author":
                if len(info.author): info.author += ", "
                info.author += val

            elif key == "subject" or key == "title":
                if len(info.subject): info.subject += " "
                info.subject += val

            elif key == "revision":
                if len(info.revision): info.revision += ", "
                info.revision += val

            elif key == "fixes":
                r = re.match("^[0-9]+$", val)
                if r:
                    bugid = int(val)
                    patch.fixes.append((bugid, None, None))
                    all_bugs.append(bugid)
                    continue

                r = re.match("^\\[ *([0-9]+) *\\](.*)$", val)
                if r:
                    bugid, description = int(r.group(1)), r.group(2).strip()
                    patch.fixes.append((bugid, None, description))   
                    all_bugs.append(bugid)
                    continue

                patch.fixes.append((None, None, val))

            elif key == "depends":
                if not name_to_id.has_key(val):
                    raise PatchUpdaterError("Definition file %s references unknown dependency %s" % (deffile, val))
                patch.depends.add(name_to_id[val])

            else:
                print "WARNING: Ignoring unknown command in definition file %s: %s" % (deffile, line)

        if len(info.author) and len(info.subject) and len(info.revision):
            patch.authors.append(info)

    # In a third step query information for the patches from Wine bugzilla
    pool = Pool(8)

    bug_short_desc = {None:None}
    for bugid, data in zip(all_bugs, pool.map(download, ["http://bugs.winehq.org/show_bug.cgi?id=%d&ctype=xml&field=short_desc" % bugid for bugid in all_bugs])):
        bug_short_desc[bugid] = minidom.parseString(data).getElementsByTagName('short_desc')[0].firstChild.data

    # The following command triggers a (harmless) python bug, which would confuse the user:
    # > Exception RuntimeError: RuntimeError('cannot join current thread',) in <Finalize object, dead> ignored
    # To avoid that just keep the pool until it destroyed by the garbage collector.
    # pool.close()

    for i, patch in all_patches.iteritems():
        patch.fixes = [(bugid, bug_short_desc[bugid], description) for bugid, dummy, description in patch.fixes]

    return all_patches

def causal_time_combine(a, b):
    """Combines two timestamps into a new one."""
    return [max(a, b) for a, b in zip(a, b)]

def causal_time_smaller(a, b):
    """Checks if timestamp a is smaller than timestamp b."""
    return all([i <= j for i, j in zip(a,b)]) and any([i < j for i, j in zip(a,b)])

def causal_time_relation(all_patches, indices):
    """Checks if the patches with given indices are applied in a very specific order."""

    def _pairs(a):
        for i, j in enumerate(a):
            for k in a[i+1:]:
                yield (j, k)

    for i, j in _pairs(indices):
        if not (causal_time_smaller(all_patches[i].verify_time, all_patches[j].verify_time) or \
                causal_time_smaller(all_patches[j].verify_time, all_patches[i].verify_time)):
            return False
    return True

def causal_time_permutations(all_patches, indices, filename):
    """Iterate over all possible permutations of patches affecting
       a specific file, which are compatible with dependencies."""
    for perm in itertools.permutations(indices):
        for i, j in zip(perm[:-1], perm[1:]):
            if causal_time_smaller(all_patches[j].verify_time, all_patches[i].verify_time):
                break
        else:
            selected_patches = []
            for i in perm:
                selected_patches += [patch for patch in all_patches[i].patches if patch.modified_file == filename]
            yield selected_patches

def contains_binary_patch(all_patches, indices, filename):
    """Checks if any patch with given indices affecting filename is a binary patch."""
    for i in indices:
        for patch in all_patches[i].patches:
            if patch.modified_file == filename and patch.is_binary():
                return True
    return False

def load_patch_cache():
    """Load dictionary for cached patch dependency tests into cached_patch_result."""
    global cached_patch_result
    try:
        with open("./.depcache") as fp:
            cached_patch_result = pickle.load(fp)
    except IOError:
        cached_patch_result = {}

def save_patch_cache():
    """Save dictionary for cached patch depdency tests."""
    with open("./.depcache", "wb") as fp:
        pickle.dump(cached_patch_result, fp, pickle.HIGHEST_PROTOCOL)

def verify_patch_order(all_patches, indices, filename):
    """Checks if the dependencies are defined correctly by applying on the patches on a copy from the git tree."""
    global cached_patch_result

    # If one of patches is a binary patch, then we cannot / won't verify it - require dependencies in this case
    if contains_binary_patch(all_patches, indices, filename):
        if not causal_time_relation(all_patches, indices):
            raise PatchUpdaterError("Because of binary patch modifying file %s the following patches need explicit dependencies: %s" %
                                    (filename, ", ".join([all_patches[i].name for i in indices])))
        return

    # Grab original file from the wine git repository - please note we grab from origin/master, not the current branch
    try:
        with open(os.devnull, 'w') as devnull:
            original_content = subprocess.check_output(["git", "show", "origin/master:%s" % filename],
                                                       cwd="./debian/tools/wine", stderr=devnull)
    except subprocess.CalledProcessError as e:
        if e.returncode != 128: raise # not found
        original_content = ""

    # Calculate hash of original content
    original_content_hash = hashlib.sha256(original_content).digest()

    # Check for possible ways to apply the patch
    failed_to_apply   = False
    last_result_hash  = None
    for patches in causal_time_permutations(all_patches, indices, filename):

        # Calculate unique hash based on the original content and the order in which the patches are applied
        m = hashlib.sha256()
        m.update(original_content_hash)
        for patch in patches:
            m.update(patch.hash())
        unique_hash = m.digest()

        # Fast path -> we know that it applies properly
        if cached_patch_result.has_key(unique_hash):
            result_hash = cached_patch_result[unique_hash]

        else:
            # Apply the patches (without fuzz)
            try:
                content = patchutils.apply_patch(original_content, patches, fuzz=0)
            except patchutils.PatchApplyError:
                if last_result_hash is not None:
                    break
                # We failed to apply the patches, but don't know if it works at all - continue
                failed_to_apply = True
                continue

            # Get hash of resulting file and add to cache
            result_hash = hashlib.sha256(content).digest()
            cached_patch_result[unique_hash] = result_hash

        # First time we got a successful result
        if last_result_hash is None:
            last_result_hash = result_hash
            if failed_to_apply: break
        # All the other times: hash to match with previous attempt
        elif last_result_hash != result_hash:
            last_result_hash = None
            break

    if failed_to_apply and last_result_hash is None:
        raise PatchUpdaterError("Changes to file %s don't apply on git source tree: %s" %
                                (filename, ", ".join([all_patches[i].name for i in indices])))

    elif failed_to_apply or last_result_hash is None:
        raise PatchUpdaterError("Depending on the order some changes to file %s dont't apply / lead to different results: %s" %
                                (filename, ", ".join([all_patches[i].name for i in indices])))

    else:
        assert len(last_result_hash) == 32

def verify_dependencies(all_patches):
    """Resolve dependencies, and afterwards run verify_patch_order() to check if everything applies properly."""
    max_patches = max(all_patches.keys()) + 1

    for i, patch in all_patches.iteritems():
        patch.verify_depends = set(patch.depends)
        patch.verify_time    = [0]*max_patches

    # Check for circular dependencies and perform modified vector clock algorithm
    patches = dict(all_patches)
    while len(patches):

        to_delete = []
        for i, patch in patches.iteritems():
            if len(patch.verify_depends) == 0:
                patch.verify_time[i] += 1
                to_delete.append(i)

        if len(to_delete) == 0:
            raise PatchUpdaterError("Circular dependency in set of patches: %d" %
                                    ", ".join([patch.name for i, patch in patches.iteritems()]))

        for j in to_delete:
            for i, patch in patches.iteritems():
                if i != j and j in patch.verify_depends:
                    patch.verify_time = causal_time_combine(patch.verify_time, patches[j].verify_time)
                    patch.verify_depends.remove(j)
            del patches[j]

    # Find out which files are modified by multiple patches
    modified_files = {}
    for i, patch in all_patches.iteritems():
        for f in patch.modified_files:
            if f not in modified_files:
                modified_files[f] = []
            modified_files[f].append(i)

    # Iterate over pairs of patches, check for existing causal relationship
    load_patch_cache()
    try:
        for f, indices in modified_files.iteritems():
            verify_patch_order(all_patches, indices, f)
    finally:
        save_patch_cache()

def generate_makefile(all_patches, fp):
    """Generate Makefile for a specific set of patches."""

    fp.write("#\n")
    fp.write("# This file is automatically generated, DO NOT EDIT!\n")
    fp.write("#\n")
    fp.write("\n")
    fp.write("CURDIR ?= ${.CURDIR}\n")
    fp.write("PATCH := $(CURDIR)/../debian/tools/gitapply.sh -d $(DESTDIR)\n")
    fp.write("\n")
    fp.write("PATCHLIST :=\t%s\n" % " \\\n\t\t".join(["%s.ok" % patch.name for i, patch in all_patches.iteritems()]))
    fp.write("\n")
    fp.write(".PHONY: install\n")
    fp.write("install:\n")
    fp.write("\t@$(MAKE) apply; \\\n")
    fp.write("\tstatus=$$?;     \\\n")
    fp.write("\trm -f *.ok;     \\\n")
    fp.write("\texit $$status\n")
    fp.write("\n")
    fp.write(".PHONY: apply\n")
    fp.write("apply: $(PATCHLIST)\n")
    fp.write("\tcat *.ok | sort | $(CURDIR)/../debian/tools/patchlist.sh | $(PATCH)\n")
    fp.write("\tcd $(DESTDIR) && autoreconf -f\n")
    fp.write("\tcd $(DESTDIR) && ./tools/make_requests\n")
    fp.write("\n")
    fp.write(".PHONY: clean\n")
    fp.write("clean:\n")
    fp.write("\trm -f *.ok\n")
    fp.write("\n")
    fp.write(".NOTPARALLEL:\n")
    fp.write("\n")

    for i, patch in all_patches.iteritems():
        fp.write("# Patchset %s\n" % patch.name)
        fp.write("# |\n")
        fp.write("# | Included patches:\n")

        # List all patches and their corresponding authors
        for info in patch.authors:
            if not info.subject: continue
            s = []
            if info.revision and info.revision != "1": s.append("rev %s" % info.revision)
            if info.author: s.append("by %s" % info.author)
            if len(s): s = " [%s]" % ", ".join(s)
            fp.write("# |   *\t%s\n" % "\n# | \t".join(textwrap.wrap(info.subject + s, 120)))
        fp.write("# |\n")

        # List all bugs fixed by this patchset
        if any([bugid is not None for bugid, bugname, description in patch.fixes]):
            fp.write("# | This patchset fixes the following Wine bugs:\n")
            for bugid, bugname, description in patch.fixes:
                if bugid is not None:
                    fp.write("# |   *\t%s\n" % "\n# | \t".join(textwrap.wrap("[#%d] %s" % (bugid, bugname), 120)))
            fp.write("# |\n")

        # List all modified files
        fp.write("# | Modified files: \n")
        fp.write("# |   *\t%s\n" % "\n# | \t".join(textwrap.wrap(", ".join(sorted(patch.modified_files)), 120)))
        fp.write("# |\n")

        # Generate dependencies and code to apply patches
        depends = " ".join([""] + ["%s.ok" % all_patches[d].name for d in patch.depends]) if len(patch.depends) else ""
        fp.write("%s.ok:%s\n" % (patch.name, depends))
        for f in patch.files:
            fp.write("\t$(PATCH) < %s\n" % os.path.join(patch.name, f))

        # Create *.ok file (used to generate patchlist)
        if len(patch.authors):
            fp.write("\t( \\\n")
            for info in patch.authors:
                if not info.subject: continue
                s = info.subject
                if info.revision and info.revision != "1": s += " [rev %s]" % info.revision
                fp.write("\t\techo \"+    { \\\"%s\\\", \\\"%s\\\", \\\"%s\\\" },\"; \\\n" % (patch.name, info.author, s))
            fp.write("\t) > %s.ok\n" % patch.name)
        else:
            fp.write("\ttouch %s.ok\n" % patch.name)
        fp.write("\n");

README_template = """wine-compholio
==============

The Wine \"Compholio\" Edition repository includes a variety of patches for
Wine to run common Windows applications under Linux.

These patches fix the following Wine bugs:

{bugs}

Besides that the following additional changes are included:

{fixes}

### Compiling wine-compholio

In order to build wine-compholio, please use the recommended Makefile based approach 
which will automatically decide whether to use 'git apply' or 'gitapply.sh'. The following
instructions (based on the [Gentoo Wiki](https://wiki.gentoo.org/wiki/Netflix/Pipelight#Compiling_manually))
will give a short overview how to compile wine-compholio, but of course not explain
all details. Make sure to install all required Wine dependencies before proceeding.

As the first step please grab the latest Wine source:
```bash
wget http://prdownloads.sourceforge.net/wine/wine-{version}.tar.bz2
wget https://github.com/compholio/wine-compholio-daily/archive/v{version}.tar.gz
```
Extract the archives:
```bash
tar xvjf wine-1*.tar.bz2
cd wine-1*
tar xvzf ../v{version}.tar.gz --strip-components 1
```
And apply the patches:
```bash
make -C ./patches DESTDIR=$(pwd) install
```
Afterwards run configure (you can also specify a prefix if you don't want to install
wine-compholio system-wide):
```bash
./configure --with-xattr
```
Before you continue you should make sure that ./configure doesn't show any warnings
(look at the end of the output). If there are any warnings, this most likely means
that you're missing some important header files. Install them and repeat the ./configure
step until all problems are fixed.

Afterwards compile it (and grab a cup of coffee):
```bash
make
```
And install it (you only need sudo for a system-wide installation):
```bash
sudo make install
```

### Excluding patches

It is also possible to apply only a subset of the patches, for example if you're compiling
for a distribution where PulseAudio is not installed, or if you just don't like a specific
patchset. Please note that some patchsets depend on each other, and requesting an impossible
situation might result in a failure to apply all patches.

Lets assume you want to exclude the patchset in directory DIRNAME, then just invoke make like that:
```bash
make -C ./patches DESTDIR=$(pwd) install -W DIRNAME.ok
```
"""

def generate_readme(all_patches, fp):
    """Generate README.md including information about specific patches and bugfixes."""

    # Get list of all bugs
    def _all_bugs():
        all_bugs = []
        for i, patch in all_patches.iteritems():
            for (bugid, bugname, description) in patch.fixes:
                if bugid is not None: all_bugs.append((bugid, bugname, description))
        for (bugid, bugname, description) in sorted(all_bugs):
            if description is None: description = bugname
            yield "%s ([Wine Bug #%d](http://bugs.winehq.org/show_bug.cgi?id=%d \"%s\"))" % (description, bugid, bugid, bugname)

    # Get list of all fixes
    def _all_fixes():
        all_fixes = []
        for i, patch in all_patches.iteritems():
            for (bugid, bugname, description) in patch.fixes:
                if bugid is None: all_fixes.append(description)
        for description in sorted(all_fixes):
            yield description

    # Create enumeration from list
    def _enum(x):
        return "* " + "\n* ".join(x)

    # Read information from changelog
    def _read_changelog():
        with open("debian/changelog") as fp:
            for line in fp:
                r = re.match("^([a-zA-Z0-9][^(]*)\((.*)\) ([^;]*)", line)
                if r: yield (r.group(1).strip(), r.group(2).strip(), r.group(3).strip())

    # Get version number of the latest stable release
    def _latest_stable_version():
        for package, version, distro in _read_changelog():
            if distro.lower() != "unreleased":
                return version

    fp.write(README_template.format(bugs=_enum(_all_bugs()), fixes=_enum(_all_fixes()), version=_latest_stable_version()))

if __name__ == "__main__":
    if not os.path.isdir("./debian/tools/wine"):
        raise RuntimeError("Please create a symlink to the wine repository in ./debian/tools/wine")

    try:
        all_patches = read_patchsets("./patches")
        verify_dependencies(all_patches)
    except PatchUpdaterError as e:
        print ""
        print "ERROR: %s" % e
        print ""
        exit(1)


    with open("./patches/Makefile", "w") as fp:
        generate_makefile(all_patches, fp)

    with open("./README.md", "w") as fp:
        generate_readme(all_patches, fp)
