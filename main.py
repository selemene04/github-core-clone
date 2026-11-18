#!/usr/bin/env python3
"""Selemene Forge — a standalone GitHub-compatible git forge in one process.

Serves, from its own in-memory store:
  * git smart HTTP (clone / push, protocol v0 and v2),
  * the GitHub REST v3 API subset (repos, contents, branches, issues, pulls),
  * a GraphQL v4 subset of the shape `gh` and github-mcp-server send,
  * MCP tools over Streamable HTTP (legacy handshake era).

Python 3 standard library only. No git binary, no files, no network calls out.

Lifecycle (make.fun harness): when MAKEFUN_ENV_URL is set, bind an ephemeral
port, POST {"port": N} to $MAKEFUN_ENV_URL/register, then poll
$MAKEFUN_ENV_URL/done until it answers {"done": true} and exit 0. Without it,
serve standalone on $FORGE_PORT (or an ephemeral port) for local testing.

Determinism: every timestamp comes from a logical clock (a counter rooted at a
fixed epoch), every id from a counter, so two runs over the same request
sequence produce identical state.
"""

import base64
import difflib
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import zlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------------
# identity / constants

# The token owner's login on this forge (the suite's scenario viewer).
VIEWER_LOGIN = "adaiversen"
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"
VIEWER_ID = 1000

ZERO_SHA = "0" * 40
EPOCH = 1_700_000_000  # fixed root of the logical clock
SERVER_VERSION = "1.0.0"
FORGE_NAME = "selemene-forge"

EXPECTED_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")

# --------------------------------------------------------------------------
# logical clock + id counters (determinism)

_lock = threading.RLock()
_clock = [0]
_repo_id_counter = [5000]
_issue_id_counter = [90000]
_comment_id_counter = [700000]
_label_id_counter = [3000]
_request_counter = [0]


def tick():
    _clock[0] += 1
    return EPOCH + _clock[0]


def iso8601(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def next_repo_id():
    value = _repo_id_counter[0]
    _repo_id_counter[0] += 1
    return value


def next_issue_id():
    value = _issue_id_counter[0]
    _issue_id_counter[0] += 1
    return value


def next_comment_id():
    value = _comment_id_counter[0]
    _comment_id_counter[0] += 1
    return value


def next_label_id():
    value = _label_id_counter[0]
    _label_id_counter[0] += 1
    return value


def next_request_id():
    _request_counter[0] += 1
    return "req-%06d" % _request_counter[0]


# --------------------------------------------------------------------------
# git object primitives (SHA-1 object model, loose objects in memory)

OBJ_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
OBJ_TYPE_IDS = {v: k for k, v in OBJ_TYPES.items()}


def hash_object(otype, data):
    header = ("%s %d" % (otype, len(data))).encode() + b"\x00"
    return hashlib.sha1(header + data).hexdigest()


def parse_tree(data):
    """Tree bytes -> [(mode_str, name, sha_hex)]."""
    entries = []
    i = 0
    while i < len(data):
        sp = data.index(b" ", i)
        mode = data[i:sp].decode()
        nul = data.index(b"\x00", sp)
        name = data[sp + 1:nul].decode()
        sha = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        i = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _sha = entry
    return name + "/" if mode == "40000" else name


def build_tree(entries):
    """[(mode, name, sha)] -> tree bytes, git sort order."""
    out = bytearray()
    for mode, name, sha in sorted(entries, key=_tree_sort_key):
        out += mode.encode() + b" " + name.encode() + b"\x00" + bytes.fromhex(sha)
    return bytes(out)


def parse_commit(data):
    """Commit bytes -> dict(tree, parents, author, committer, message)."""
    head, _, message = data.partition(b"\n\n")
    result = {"tree": None, "parents": [], "author": "", "committer": "", "message": message.decode("utf-8", "replace")}
    for line in head.decode("utf-8", "replace").split("\n"):
        if line.startswith("tree "):
            result["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            result["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            result["author"] = line[7:]
        elif line.startswith("committer "):
            result["committer"] = line[10:]
    return result


def build_commit(tree_sha, parents, message, epoch):
    ident = "%s <%s> %d +0000" % (VIEWER_NAME, VIEWER_EMAIL, epoch)
    lines = ["tree " + tree_sha]
    for parent in parents:
        lines.append("parent " + parent)
    lines.append("author " + ident)
    lines.append("committer " + ident)
    # The message is stored verbatim: the REST commits view must read back
    # exactly the message the contents API was given.
    text = "\n".join(lines) + "\n\n" + message
    return text.encode()


# --------------------------------------------------------------------------
# packfiles

def build_pack(objects):
    """[(type, data)] -> pack bytes (plain objects, no deltas)."""
    out = bytearray()
    out += b"PACK" + (2).to_bytes(4, "big") + len(objects).to_bytes(4, "big")
    for otype, data in objects:
        type_id = OBJ_TYPE_IDS[otype]
        size = len(data)
        first = (type_id << 4) | (size & 0x0F)
        size >>= 4
        if size:
            first |= 0x80
        out.append(first)
        while size:
            byte = size & 0x7F
            size >>= 7
            if size:
                byte |= 0x80
            out.append(byte)
        out += zlib.compress(data)
    trailer = hashlib.sha1(bytes(out)).digest()
    return bytes(out) + trailer


def _apply_delta(base, delta):
    pos = 0

    def read_varint():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                return value

    src_size = read_varint()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    _dst_size = read_varint()
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    return bytes(out)


def parse_pack(data, store_lookup):
    """Pack bytes -> {sha: (type, data)}; resolves OFS/REF deltas.

    store_lookup(sha) -> (type, data) | None, for thin-pack bases.
    """
    if len(data) < 12 + 20 or data[:4] != b"PACK":
        raise ValueError("not a pack")
    count = int.from_bytes(data[8:12], "big")
    pos = 12
    raw = []  # (offset, type_id, payload) payload: bytes | ('ofs', neg, delta) | ('ref', sha, delta)
    for _ in range(count):
        offset = pos
        byte = data[pos]
        pos += 1
        type_id = (byte >> 4) & 0x07
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = data[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        if type_id == 6:  # OFS_DELTA
            byte = data[pos]
            pos += 1
            neg = byte & 0x7F
            while byte & 0x80:
                byte = data[pos]
                pos += 1
                neg = ((neg + 1) << 7) | (byte & 0x7F)
            decomp = zlib.decompressobj()
            delta = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            raw.append((offset, type_id, ("ofs", neg, delta)))
        elif type_id == 7:  # REF_DELTA
            base_sha = data[pos:pos + 20].hex()
            pos += 20
            decomp = zlib.decompressobj()
            delta = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            raw.append((offset, type_id, ("ref", base_sha, delta)))
        else:
            decomp = zlib.decompressobj()
            payload = decomp.decompress(data[pos:])
            pos += len(data[pos:]) - len(decomp.unused_data)
            if len(payload) != size:
                raise ValueError("pack entry size mismatch")
            raw.append((offset, type_id, payload))
    objects = {}
    by_offset = {}

    def resolve(entry):
        offset, type_id, payload = entry
        if isinstance(payload, bytes):
            otype = OBJ_TYPES[type_id]
            sha = hash_object(otype, payload)
            return sha, otype, payload
        kind, ref, delta = payload
        if kind == "ofs":
            base_entry = by_offset[offset - ref]
            _bsha, btype, bdata = resolve(base_entry)
        else:
            found = None
            for candidate in raw:
                if isinstance(candidate[2], bytes):
                    csha = hash_object(OBJ_TYPES[candidate[1]], candidate[2])
                    if csha == ref:
                        found = (csha, OBJ_TYPES[candidate[1]], candidate[2])
                        break
            if found is None:
                found = store_lookup(ref)
                if found is None:
                    raise ValueError("delta base missing")
                found = (ref, found[0], found[1])
            _bsha, btype, bdata = found
        result = _apply_delta(bdata, delta)
        sha = hash_object(btype, result)
        return sha, btype, result

    for entry in raw:
        by_offset[entry[0]] = entry
    for entry in raw:
        sha, otype, payload = resolve(entry)
        objects[sha] = (otype, payload)
    return objects


# --------------------------------------------------------------------------
# pkt-line codec

def pkt_line(payload):
    if payload is None:  # flush
        return b"0000"
    if payload == "delim":
        return b"0001"
    if payload == "resp-end":
        return b"0002"
    length = len(payload) + 4
    return ("%04x" % length).encode() + payload


def pkt_data(text):
    return pkt_line(text.encode())


def parse_pkt_lines(data):
    """Bytes -> (lines, rest). Lines are payload bytes; 'flush'/'delim' markers."""
    lines = []
    pos = 0
    while pos + 4 <= len(data):
        try:
            length = int(data[pos:pos + 4].decode(), 16)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("bad pkt-line header")
        if length == 0:
            lines.append("flush")
            pos += 4
            break
        if length == 1:
            lines.append("delim")
            pos += 4
            continue
        if length == 2:
            lines.append("resp-end")
            pos += 4
            continue
        if length < 4 or pos + length > len(data):
            raise ValueError("bad pkt-line length")
        lines.append(data[pos + 4:pos + length])
        pos += length
    return lines, data[pos:]


# --------------------------------------------------------------------------
# the store

class Repo:
    def __init__(self, name, private, description):
        self.id = next_repo_id()
        self.name = name
        self.owner = VIEWER_LOGIN
        self.private = bool(private)
        self.description = description or ""
        self.default_branch = "main"
        self.objects = {}          # sha -> (type, data bytes)
        self.refs = {}             # "refs/heads/main" -> sha
        self.labels = {}           # name -> {id, name, color, description}
        self.issues = {}           # number -> issue dict
        self.pulls = {}            # number -> pull dict
        self.next_number = 1       # issues and pulls share one sequence
        self.created_epoch = tick()

    # -- objects ------------------------------------------------------
    def put(self, otype, data):
        sha = hash_object(otype, data)
        if sha not in self.objects:
            self.objects[sha] = (otype, data)
        return sha

    def get(self, sha):
        return self.objects.get(sha)

    def head_ref(self):
        return "refs/heads/" + self.default_branch

    def branch_tip(self, branch):
        return self.refs.get("refs/heads/" + branch)

    def peel_to_commit(self, sha):
        seen = set()
        while sha and sha not in seen:
            seen.add(sha)
            obj = self.objects.get(sha)
            if obj is None:
                return None
            otype, data = obj
            if otype == "commit":
                return sha
            if otype == "tag":
                for line in data.decode("utf-8", "replace").split("\n"):
                    if line.startswith("object "):
                        sha = line[7:].strip()
                        break
                else:
                    return None
            else:
                return None
        return None

    def peel_tag(self, sha):
        """If sha is an annotated tag, the commit it points at, else None."""
        obj = self.objects.get(sha)
        if obj is None or obj[0] != "tag":
            return None
        return self.peel_to_commit(sha)


STORE = {}  # name -> Repo


def find_repo(owner, name):
    repo = STORE.get(name)
    if repo is None or repo.owner != owner:
        return None
    return repo


def find_repo_by_name(name):
    return STORE.get(name)


def find_repo_by_id(repo_id):
    for repo in STORE.values():
        if repo.id == repo_id:
            return repo
    return None


# --------------------------------------------------------------------------
# tree walking / editing

def flatten_tree(repo, tree_sha, prefix=""):
    """{path: (mode, sha)} for every file under a tree (recursive)."""
    out = {}
    if not tree_sha:
        return out
    obj = repo.get(tree_sha)
    if obj is None or obj[0] != "tree":
        return out
    for mode, name, sha in parse_tree(obj[1]):
        path = prefix + name
        if mode == "40000":
            out.update(flatten_tree(repo, sha, path + "/"))
        else:
            out[path] = (mode, sha)
    return out


def list_dir(repo, tree_sha, prefix=""):
    """Immediate entries of a tree: [(mode, name, sha, full_path)]."""
    obj = repo.get(tree_sha) if tree_sha else None
    if obj is None or obj[0] != "tree":
        return []
    return [(mode, name, sha, prefix + name) for mode, name, sha in parse_tree(obj[1])]


def tree_at_path(repo, root_tree_sha, dir_path):
    """The tree sha at a directory path ('' = root), or None."""
    if not dir_path:
        return root_tree_sha
    current = root_tree_sha
    for segment in dir_path.split("/"):
        obj = repo.get(current) if current else None
        if obj is None or obj[0] != "tree":
            return None
        nxt = None
        for mode, name, sha in parse_tree(obj[1]):
            if name == segment and mode == "40000":
                nxt = sha
                break
        if nxt is None:
            return None
        current = nxt
    return current


def set_in_tree(repo, tree_sha, segments, blob_sha):
    """Return a new tree sha with segments->blob_sha set (None deletes)."""
    entries = []
    obj = repo.get(tree_sha) if tree_sha else None
    if obj is not None and obj[0] == "tree":
        entries = list(parse_tree(obj[1]))
    name = segments[0]
    if len(segments) == 1:
        entries = [e for e in entries if e[1] != name]
        if blob_sha is not None:
            entries.append(("100644", name, blob_sha))
    else:
        sub_sha = None
        rest = []
        for mode, entry_name, sha in entries:
            if entry_name == name and mode == "40000":
                sub_sha = sha
            else:
                rest.append((mode, entry_name, sha))
        new_sub = set_in_tree(repo, sub_sha, segments[1:], blob_sha)
        entries = rest
        if new_sub is not None:
            entries.append(("40000", name, new_sub))
    if not entries:
        return None
    return repo.put("tree", build_tree(entries))


def commit_tree(repo, branch, tree_sha, parents, message):
    epoch = tick()
    data = build_commit(tree_sha, parents, message, epoch)
    return repo.put("commit", data)


def apply_file_change(repo, branch, path, new_blob_sha, message):
    """Commit a single-file change on a branch; creates the branch from the
    default branch (or unborn) when it does not exist yet."""
    ref = "refs/heads/" + branch
    tip = repo.refs.get(ref)
    if tip is None:
        base_tip = repo.refs.get(repo.head_ref())
        if base_tip is not None:
            repo.refs[ref] = base_tip
            tip = base_tip
    parents = []
    old_tree = None
    if tip is not None:
        commit = parse_commit(repo.get(tip)[1])
        old_tree = commit["tree"]
        parents = [tip]
    new_tree = set_in_tree(repo, old_tree, path.split("/"), new_blob_sha)
    if new_tree is None:  # deleting the last file: empty tree
        new_tree = repo.put("tree", b"")
    commit_sha = commit_tree(repo, branch, new_tree, parents, message)
    repo.refs[ref] = commit_sha
    return commit_sha


def commit_parents(repo, sha):
    obj = repo.get(sha)
    if obj is None or obj[0] != "commit":
        return []
    return parse_commit(obj[1])["parents"]


def ancestors_with_dist(repo, tip):
    """{commit_sha: shortest distance from tip} (tip itself = 0)."""
    dist = {}
    if tip is None:
        return dist
    queue = deque([(tip, 0)])
    while queue:
        sha, depth = queue.popleft()
        if sha in dist and dist[sha] <= depth:
            continue
        dist[sha] = depth
        for parent in commit_parents(repo, sha):
            queue.append((parent, depth + 1))
    return dist


def is_ancestor(repo, old, new):
    if old == new:
        return True
    return old in ancestors_with_dist(repo, repo.peel_to_commit(new) or new)


def merge_base(repo, a, b):
    anc_a = ancestors_with_dist(repo, a)
    anc_b = ancestors_with_dist(repo, b)
    common = set(anc_a) & set(anc_b)
    if not common:
        return None
    return min(common, key=lambda sha: anc_a[sha] + anc_b[sha])


def three_way_merge(repo, base_tip, head_tip):
    """Returns (merged_flat_tree, conflicts). flat tree: {path: (mode, sha)}."""
    base_commit = repo.peel_to_commit(base_tip)
    head_commit = repo.peel_to_commit(head_tip)
    mb = merge_base(repo, base_commit, head_commit) if base_commit and head_commit else None
    mb_flat = flatten_tree(repo, parse_commit(repo.get(mb)[1])["tree"]) if mb else {}
    base_flat = flatten_tree(repo, parse_commit(repo.get(base_commit)[1])["tree"]) if base_commit else {}
    head_flat = flatten_tree(repo, parse_commit(repo.get(head_commit)[1])["tree"]) if head_commit else {}
    merged = {}
    conflicts = []
    for path in set(mb_flat) | set(base_flat) | set(head_flat):
        mb_v = mb_flat.get(path)
        base_v = base_flat.get(path)
        head_v = head_flat.get(path)
        if head_v == base_v:
            chosen = head_v
        elif base_v == mb_v:
            chosen = head_v
        elif head_v == mb_v:
            chosen = base_v
        else:
            conflicts.append(path)
            continue
        if chosen is not None:
            merged[path] = chosen
    return merged, conflicts


def flat_to_tree(repo, flat):
    root = None
    # rebuild bottom-up: group by directory
    dirs = {}
    for path, (mode, sha) in flat.items():
        parts = path.split("/")
        dirs.setdefault("/".join(parts[:-1]), []).append((mode, parts[-1], sha))
    tree_shas = {}

    def build(dir_path):
        entries = list(dirs.get(dir_path, []))
        subdirs = set()
        for other in dirs:
            if other == dir_path:
                continue
            parent = "/".join(other.split("/")[:-1])
            if parent == dir_path:
                subdirs.add(other)
        for sub in sorted(subdirs):
            name = sub.split("/")[-1]
            entries.append(("40000", name, build(sub)))
        if not entries and dir_path != "":
            return None
        sha = repo.put("tree", build_tree(entries))
        tree_shas[dir_path] = sha
        return sha

    root = build("")
    if root is None:
        root = repo.put("tree", b"")
    return root


def diff_flats(repo, old_flat, new_flat):
    """[{filename, status, additions, deletions, changes}] between two flat trees."""
    out = []

    def lines_of(sha):
        if sha is None:
            return []
        obj = repo.get(sha)
        if obj is None:
            return []
        return obj[1].decode("utf-8", "replace").splitlines()

    paths = sorted(set(old_flat) | set(new_flat))
    for path in paths:
        old = old_flat.get(path)
        new = new_flat.get(path)
        if old == new:
            continue
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        else:
            status = "modified"
        old_lines = lines_of(old[1] if old else None)
        new_lines = lines_of(new[1] if new else None)
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        additions = deletions = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("insert", "replace"):
                additions += j2 - j1
            if tag in ("delete", "replace"):
                deletions += i2 - i1
        out.append({
            "filename": path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
        })
    return out


# --------------------------------------------------------------------------
# REST serializers

def user_json():
    return {"login": VIEWER_LOGIN, "id": VIEWER_ID, "name": VIEWER_NAME,
            "email": VIEWER_EMAIL, "type": "User"}


def repo_json(repo):
    return {
        "id": repo.id,
        "node_id": global_id("Repository", repo.id),
        "name": repo.name,
        "full_name": "%s/%s" % (repo.owner, repo.name),
        "owner": user_json(),
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "has_issues": True,
        "open_issues_count": sum(1 for i in repo.issues.values() if i["state"] == "open"),
        "created_at": iso8601(repo.created_epoch),
        "updated_at": iso8601(repo.created_epoch),
        "html_url": "http://forge.local/%s/%s" % (repo.owner, repo.name),
        "url": "http://forge.local/api/v3/repos/%s/%s" % (repo.owner, repo.name),
    }


def label_json(label):
    return {"id": label["id"], "name": label["name"], "color": label["color"],
            "description": label.get("description")}


def issue_json(repo, issue):
    return {
        "id": issue["id"],
        "node_id": global_id("Issue", issue["id"]),
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"] if issue["body"] is not None else "",
        "state": issue["state"],
        "labels": [label_json(repo.labels[name]) for name in issue["labels"] if name in repo.labels],
        "user": user_json(),
        "comments": len(issue["comments"]),
        "created_at": iso8601(issue["created_epoch"]),
        "updated_at": iso8601(issue["updated_epoch"]),
        "html_url": "http://forge.local/%s/%s/issues/%d" % (repo.owner, repo.name, issue["number"]),
    }


def comment_json(comment):
    return {
        "id": comment["id"],
        "node_id": global_id("IssueComment", comment["id"]),
        "body": comment["body"],
        "user": user_json(),
        "created_at": iso8601(comment["created_epoch"]),
        "updated_at": iso8601(comment["created_epoch"]),
    }


def pull_json(repo, pull):
    head_sha = repo.branch_tip(pull["head"]) or ZERO_SHA
    base_sha = repo.branch_tip(pull["base"]) or ZERO_SHA
    return {
        "id": pull["id"],
        "node_id": global_id("PullRequest", pull["id"]),
        "number": pull["number"],
        "state": pull["state"],
        "title": pull["title"],
        "body": pull["body"] if pull["body"] is not None else "",
        "user": user_json(),
        "head": {"label": "%s:%s" % (repo.owner, pull["head"]), "ref": pull["head"], "sha": head_sha},
        "base": {"label": "%s:%s" % (repo.owner, pull["base"]), "ref": pull["base"], "sha": base_sha},
        "merged": pull["merged"],
        "merged_at": pull["merged_at"],
        "merge_commit_sha": pull["merge_commit_sha"],
        "created_at": iso8601(pull["created_epoch"]),
        "updated_at": iso8601(pull["updated_epoch"]),
        "html_url": "http://forge.local/%s/%s/pull/%d" % (repo.owner, repo.name, pull["number"]),
    }


# --------------------------------------------------------------------------
# domain operations

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def create_issue(repo, title, body, label_names):
    for name in label_names or []:
        if name not in repo.labels:
            raise ApiError(422, "Validation Failed: label '%s' does not exist" % name)
    number = repo.next_number
    repo.next_number += 1
    epoch = tick()
    issue = {
        "id": next_issue_id(),
        "number": number,
        "title": title,
        "body": body if body is not None else "",
        "state": "open",
        "labels": list(label_names or []),
        "comments": [],
        "created_epoch": epoch,
        "updated_epoch": epoch,
    }
    repo.issues[number] = issue
    return issue


def create_pull(repo, title, body, head, base):
    head = (head or "").split(":")[-1]
    base = (base or "").split(":")[-1]
    if repo.branch_tip(head) is None:
        raise ApiError(422, "Validation Failed: head branch '%s' does not exist" % head)
    if repo.branch_tip(base) is None:
        raise ApiError(422, "Validation Failed: base branch '%s' does not exist" % base)
    number = repo.next_number
    repo.next_number += 1
    epoch = tick()
    pull = {
        "id": next_issue_id(),
        "number": number,
        "title": title,
        "body": body if body is not None else "",
        "state": "open",
        "head": head,
        "base": base,
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "created_epoch": epoch,
        "updated_epoch": epoch,
    }
    repo.pulls[number] = pull
    return pull


def pull_files(repo, pull):
    base_tip = repo.branch_tip(pull["base"])
    head_tip = repo.branch_tip(pull["head"])
    base_flat = {}
    head_flat = {}
    if base_tip:
        base_flat = flatten_tree(repo, parse_commit(repo.get(repo.peel_to_commit(base_tip))[1])["tree"])
    if head_tip:
        head_flat = flatten_tree(repo, parse_commit(repo.get(repo.peel_to_commit(head_tip))[1])["tree"])
    # diff against the merge base, like GitHub's three-dot diff
    if base_tip and head_tip:
        mb = merge_base(repo, repo.peel_to_commit(base_tip), repo.peel_to_commit(head_tip))
        if mb:
            base_flat = flatten_tree(repo, parse_commit(repo.get(mb)[1])["tree"])
    return diff_flats(repo, base_flat, head_flat)


def pull_conflicts(repo, pull):
    base_tip = repo.branch_tip(pull["base"])
    head_tip = repo.branch_tip(pull["head"])
    if base_tip is None or head_tip is None:
        return ["<missing branch>"]
    _merged, conflicts = three_way_merge(repo, base_tip, head_tip)
    return conflicts


def merge_pull(repo, pull, method="squash"):
    """Squash-merge a pull request. Returns the merge commit sha."""
    if pull["state"] != "open":
        raise ApiError(405, "Pull request is not open")
    base_ref = "refs/heads/" + pull["base"]
    base_tip = repo.refs.get(base_ref)
    head_tip = repo.branch_tip(pull["head"])
    if base_tip is None or head_tip is None:
        raise ApiError(405, "Pull request branch is missing")
    merged_flat, conflicts = three_way_merge(repo, base_tip, head_tip)
    if conflicts:
        raise ApiError(405, "Pull Request is not mergeable: conflicting files: %s" % ", ".join(sorted(conflicts)))
    tree_sha = flat_to_tree(repo, merged_flat)
    message = "%s (#%d)" % (pull["title"], pull["number"])
    commit_sha = commit_tree(repo, pull["base"], tree_sha, [base_tip], message)
    repo.refs[base_ref] = commit_sha
    pull["state"] = "closed"
    pull["merged"] = True
    pull["merged_at"] = iso8601(tick())
    pull["merge_commit_sha"] = commit_sha
    pull["updated_epoch"] = tick()
    return commit_sha


# --------------------------------------------------------------------------
# global ids (GraphQL Relay-style, opaque and stable)

def global_id(type_name, numeric):
    raw = "%s:%s" % (type_name, numeric)
    return base64.b64encode(raw.encode()).decode()


def parse_global_id(value):
    try:
        raw = base64.b64decode(value.encode()).decode()
        type_name, _, rest = raw.partition(":")
        return type_name, rest
    except Exception:
        return None, None


# --------------------------------------------------------------------------
# git smart HTTP

UPLOAD_CAPS_V0 = "multi_ack_detailed side-band-64k thin-pack ofs-delta agent=%s/%s" % (FORGE_NAME, SERVER_VERSION)
RECEIVE_CAPS = "report-status side-band-64k delete-refs ofs-delta agent=%s/%s" % (FORGE_NAME, SERVER_VERSION)


def sorted_refs(repo):
    names = sorted(repo.refs.keys())
    heads = [n for n in names if n.startswith("refs/heads/")]
    tags = [n for n in names if n.startswith("refs/tags/")]
    other = [n for n in names if n not in heads and n not in tags]
    return heads + tags + other


def advertise_upload_pack(repo, version2):
    if version2:
        out = bytearray()
        out += pkt_data("version 2\n")
        out += pkt_data("agent=%s/%s\n" % (FORGE_NAME, SERVER_VERSION))
        out += pkt_data("ls-refs=unborn\n")
        out += pkt_data("fetch=shallow\n")
        out += pkt_data("object-format=sha1\n")
        out += pkt_line(None)
        return bytes(out)
    out = bytearray()
    out += pkt_data("# service=git-upload-pack\n")
    out += pkt_line(None)
    caps = UPLOAD_CAPS_V0 + " symref=HEAD:%s" % repo.head_ref()
    head_tip = repo.refs.get(repo.head_ref())
    lines = []
    if head_tip:
        lines.append("%s HEAD\x00%s" % (head_tip, caps))
    else:
        lines.append("%s HEAD\x00%s" % (ZERO_SHA, caps))
    for name in sorted_refs(repo):
        sha = repo.refs[name]
        lines.append("%s %s" % (sha, name))
        peeled = repo.peel_tag(sha)
        if peeled:
            lines.append("%s %s^{}" % (peeled, name))
    if not repo.refs:
        lines = ["%s capabilities^{}\x00%s" % (ZERO_SHA, caps)]
    for line in lines:
        out += pkt_data(line + "\n")
    out += pkt_line(None)
    return bytes(out)


def advertise_receive_pack(repo):
    out = bytearray()
    out += pkt_data("# service=git-receive-pack\n")
    out += pkt_line(None)
    caps = RECEIVE_CAPS + " symref=HEAD:%s" % repo.head_ref()
    if not repo.refs:
        out += pkt_data("%s capabilities^{}\x00%s\n" % (ZERO_SHA, caps))
    else:
        first = True
        head_tip = repo.refs.get(repo.head_ref())
        lines = []
        if head_tip:
            lines.append("%s HEAD" % head_tip)
        for name in sorted_refs(repo):
            sha = repo.refs[name]
            lines.append("%s %s" % (sha, name))
            peeled = repo.peel_tag(sha)
            if peeled:
                lines.append("%s %s^{}" % (peeled, name))
        for line in lines:
            if first:
                out += pkt_data(line + "\x00" + caps + "\n")
                first = False
            else:
                out += pkt_data(line + "\n")
    out += pkt_line(None)
    return bytes(out)


def ls_refs_response(repo, args):
    prefixes = []
    want_peel = "peel" in args
    want_symrefs = "symrefs" in args
    want_unborn = "unborn" in args
    for arg in args:
        if arg.startswith("ref-prefix "):
            prefixes.append(arg[len("ref-prefix "):])

    def matches(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    out = bytearray()
    head_ref = repo.head_ref()
    head_tip = repo.refs.get(head_ref)
    if matches("HEAD"):
        if head_tip:
            line = "%s HEAD" % head_tip
            if want_symrefs:
                line += " symref-target:%s" % head_ref
            out += pkt_data(line + "\n")
        elif want_unborn:
            line = "unborn HEAD"
            if want_symrefs:
                line += " symref-target:%s" % head_ref
            out += pkt_data(line + "\n")
    for name in sorted_refs(repo):
        if not matches(name):
            continue
        sha = repo.refs[name]
        line = "%s %s" % (sha, name)
        if want_peel:
            peeled = repo.peel_tag(sha)
            if peeled:
                line += " peeled:%s" % peeled
        out += pkt_data(line + "\n")
    out += pkt_line(None)
    return bytes(out)


def _tree_closure(repo, commit_sha, acc):
    """Add a commit's tree and blobs to acc (sha -> (type, data))."""
    obj = repo.get(commit_sha)
    if obj is None:
        return
    stack = [parse_commit(obj[1])["tree"]]
    while stack:
        tree_sha = stack.pop()
        if tree_sha in acc:
            continue
        tree_obj = repo.get(tree_sha)
        if tree_obj is None:
            continue
        acc[tree_sha] = tree_obj
        for mode, _name, sha in parse_tree(tree_obj[1]):
            if mode == "40000":
                stack.append(sha)
            else:
                blob = repo.get(sha)
                if blob is not None:
                    acc[sha] = blob


def compute_fetch(repo, wants, haves, client_shallow, depth):
    """Returns (objects list[(type,data)], shallow list, unshallow list)."""
    # objects the client already holds, cut at its shallow boundary
    have_objs = {}
    have_commits = set()
    for have in haves:
        commit = repo.peel_to_commit(have)
        if commit is None:
            continue
        dist = ancestors_with_dist(repo, commit)
        for sha in dist:
            if sha in client_shallow and sha != commit:
                continue
            have_commits.add(sha)
        # ancestors beyond a shallow boundary are not held
        boundary_parents = set()
        for sha in list(have_commits):
            if sha in client_shallow:
                for parent in commit_parents(repo, sha):
                    boundary_parents.add(parent)
        have_commits -= boundary_parents
        # the boundary commits themselves are held, their ancestors are not
        for sha in client_shallow:
            if sha in dist:
                have_commits.add(sha)
    for sha in have_commits:
        obj = repo.get(sha)
        if obj is not None:
            have_objs[sha] = obj
            _tree_closure(repo, sha, have_objs)
    # also: non-commit have objects (tags) count as held
    for have in haves:
        obj = repo.get(have)
        if obj is not None:
            have_objs[have] = obj

    # distances from the wants (commits only)
    want_commits = []
    extra_objects = {}
    for want in wants:
        obj = repo.get(want)
        if obj is None:
            continue
        if obj[0] == "tag":
            extra_objects[want] = obj
            target = repo.peel_to_commit(want)
            if target:
                want_commits.append(target)
        elif obj[0] == "commit":
            want_commits.append(want)
        else:
            extra_objects[want] = obj
    dist = {}
    queue = deque()
    for tip in want_commits:
        queue.append((tip, 1))  # wants are distance 1
    while queue:
        sha, d = queue.popleft()
        if sha in dist and dist[sha] <= d:
            continue
        dist[sha] = d
        if depth is not None and d >= depth:
            continue
        for parent in commit_parents(repo, sha):
            queue.append((parent, d + 1))
    included = set()
    for sha, d in dist.items():
        if depth is not None and d > depth:
            continue
        included.add(sha)
    send = {}
    for sha in included:
        if sha in have_commits:
            continue
        obj = repo.get(sha)
        if obj is not None:
            send[sha] = obj
            _tree_closure(repo, sha, send)
    for sha, obj in extra_objects.items():
        if sha not in have_objs:
            send[sha] = obj
    for sha in list(send):
        if sha in have_objs and sha not in included:
            del send[sha]
    # shallow boundary: included commits with a parent that exists but is not included
    boundary = []
    for sha in included:
        if sha in have_commits and depth is None:
            continue
        for parent in commit_parents(repo, sha):
            if parent not in included and repo.get(parent) is not None:
                if not (depth is None and parent in have_commits):
                    boundary.append(sha)
                break
    unshallow = []
    for sha in client_shallow:
        if sha in included and sha not in boundary:
            unshallow.append(sha)
    ordered = sorted(send.items(), key=lambda kv: kv[0])
    return [obj for _sha, obj in ordered], sorted(set(boundary)), sorted(set(unshallow))


def sideband_wrap(payload, band=1):
    out = bytearray()
    chunk = 60000
    for i in range(0, max(len(payload), 1), chunk):
        piece = payload[i:i + chunk]
        out += pkt_line(bytes([band]) + piece)
    return bytes(out)


def handle_upload_pack(repo, body, version2):
    lines, _rest = parse_pkt_lines(body)
    # normalize: payloads as text lines
    texts = []
    for item in lines:
        if item in ("flush", "delim"):
            texts.append(item)
        else:
            texts.append(item.decode("utf-8", "replace").rstrip("\n"))
    if version2:
        if not texts or not texts[0].startswith("command="):
            raise ValueError("v2 request without command")
        command = texts[0][len("command="):]
        args = []
        seen_delim = False
        for item in texts[1:]:
            if item == "delim":
                seen_delim = True
                continue
            if item == "flush":
                break
            if seen_delim:
                args.append(item)
        if command == "ls-refs":
            return ls_refs_response(repo, args)
        if command == "fetch":
            wants, haves, shallow = [], [], []
            depth = None
            for arg in args:
                if arg.startswith("want "):
                    wants.append(arg[5:])
                elif arg.startswith("have "):
                    haves.append(arg[5:])
                elif arg.startswith("shallow "):
                    shallow.append(arg[8:])
                elif arg.startswith("deepen "):
                    depth = int(arg[len("deepen "):])
            objects, shallow_out, unshallow_out = compute_fetch(repo, wants, haves, shallow, depth)
            pack = build_pack(objects)
            out = bytearray()
            out += pkt_data("acknowledgments\n")
            acked = [h for h in haves if repo.get(h) is not None]
            if acked:
                out += pkt_data("ACK %s\n" % acked[0])
                out += pkt_data("ready\n")
            else:
                out += pkt_data("NAK\n")
            out += pkt_line(None)
            if shallow_out or unshallow_out:
                out += pkt_data("shallow-info\n")
                for sha in shallow_out:
                    out += pkt_data("shallow %s\n" % sha)
                for sha in unshallow_out:
                    out += pkt_data("unshallow %s\n" % sha)
                out += pkt_line(None)
            out += pkt_data("packfile\n")
            out += sideband_wrap(pack, 1)
            out += pkt_line(None)
            return bytes(out)
        raise ValueError("unknown v2 command %s" % command)
    # v0 fetch: wants section, flush, haves/done section, flush
    wants, haves, shallow = [], [], []
    depth = None
    section = 0
    for item in texts:
        if item == "flush":
            section += 1
            continue
        if section == 0:
            if item.startswith("want "):
                wants.append(item.split()[1])
            elif item.startswith("shallow "):
                shallow.append(item[8:])
            elif item.startswith("deepen "):
                depth = int(item[len("deepen "):])
        else:
            if item.startswith("have "):
                haves.append(item[5:])
    objects, shallow_out, unshallow_out = compute_fetch(repo, wants, haves, shallow, depth)
    pack = build_pack(objects)
    out = bytearray()
    for sha in shallow_out:
        out += pkt_data("shallow %s\n" % sha)
    for sha in unshallow_out:
        out += pkt_data("unshallow %s\n" % sha)
    out += pkt_data("NAK\n")
    out += sideband_wrap(pack, 1)
    out += pkt_line(None)
    return bytes(out)


def handle_receive_pack(repo, body):
    lines, rest = parse_pkt_lines(body)
    commands = []
    capabilities = ""
    for item in lines:
        if item == "flush":
            break
        text = item.decode("utf-8", "replace").rstrip("\n")
        if "\x00" in text:
            text, capabilities = text.split("\x00", 1)
        parts = text.split(" ")
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    use_sideband = "side-band-64k" in capabilities
    report_status = "report-status" in capabilities
    unpack_ok = True
    unpack_detail = "ok"
    pack_objects = {}
    needs_pack = any(new != ZERO_SHA for _old, new, _ref in commands)
    if needs_pack and rest:
        try:
            pack_objects = parse_pack(rest, lambda sha: repo.get(sha))
            for sha, obj in pack_objects.items():
                if sha not in repo.objects:
                    repo.objects[sha] = obj
        except Exception as exc:  # corrupt pack: refuse everything
            unpack_ok = False
            unpack_detail = "index-pack failed: %s" % exc
    results = []
    if unpack_ok:
        for old, new, ref in commands:
            current = repo.refs.get(ref, ZERO_SHA)
            if new == ZERO_SHA:
                if ref in repo.refs:
                    del repo.refs[ref]
                results.append((ref, True, ""))
                continue
            if repo.get(new) is None:
                results.append((ref, False, "missing object"))
                continue
            if ref.startswith("refs/heads/"):
                if current != ZERO_SHA and current != old:
                    results.append((ref, False, "reference already exists"))
                    continue
                if current != ZERO_SHA:
                    new_commit = repo.peel_to_commit(new)
                    if new_commit is None or not is_ancestor(repo, current, new_commit):
                        results.append((ref, False, "non-fast-forward"))
                        continue
                elif old != ZERO_SHA and not is_ancestor(repo, old, repo.peel_to_commit(new) or new):
                    results.append((ref, False, "non-fast-forward"))
                    continue
            repo.refs[ref] = new
            results.append((ref, True, ""))
    else:
        for _old, _new, ref in commands:
            results.append((ref, False, "unpack failed"))
    if not report_status:
        return b""
    report = bytearray()
    report += pkt_data("unpack %s\n" % unpack_detail)
    for ref, ok, reason in results:
        if ok:
            report += pkt_data("ok %s\n" % ref)
        else:
            report += pkt_data("ng %s %s\n" % (ref, reason))
    report += pkt_line(None)
    if use_sideband:
        out = bytearray()
        out += sideband_wrap(bytes(report), 1)
        out += pkt_line(None)
        return bytes(out)
    return bytes(report)


# --------------------------------------------------------------------------
# REST helpers

def paginate(items, query):
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    start = (page - 1) * per_page
    return items[start:start + per_page], page, per_page, start + per_page < len(items)


def resolve_ref_sha(repo, ref):
    """Branch name, tag name, full ref or sha -> commit sha (or None)."""
    if not ref:
        tip = repo.refs.get(repo.head_ref())
        return tip
    if ref in repo.refs:
        return repo.refs[ref]
    if ("refs/heads/" + ref) in repo.refs:
        return repo.refs["refs/heads/" + ref]
    if ("refs/tags/" + ref) in repo.refs:
        return repo.refs["refs/tags/" + ref]
    if repo.get(ref) is not None:
        return ref
    return None


def file_entry_json(repo, path, mode, sha):
    obj = repo.get(sha)
    size = len(obj[1]) if obj else 0
    return {"type": "file", "name": path.split("/")[-1], "path": path, "sha": sha,
            "size": size, "encoding": "base64"}


def content_response(repo, path, ref):
    tip = resolve_ref_sha(repo, ref)
    if tip is None:
        raise ApiError(404, "Not Found")
    commit_sha = repo.peel_to_commit(tip)
    if commit_sha is None:
        raise ApiError(404, "Not Found")
    root_tree = parse_commit(repo.get(commit_sha)[1])["tree"]
    flat = flatten_tree(repo, root_tree)
    if path in flat:
        mode, sha = flat[path]
        obj = repo.get(sha)
        return {"type": "file", "encoding": "base64",
                "content": base64.b64encode(obj[1]).decode(),
                "name": path.split("/")[-1], "path": path, "sha": sha, "size": len(obj[1])}
    dir_tree = tree_at_path(repo, root_tree, path)
    if dir_tree is not None:
        entries = []
        for mode, name, sha, full in list_dir(repo, dir_tree, (path + "/") if path else ""):
            if mode == "40000":
                entries.append({"type": "dir", "name": name, "path": full, "sha": sha})
            else:
                entries.append(file_entry_json(repo, full, mode, sha))
        return entries
    if path == "":
        entries = []
        for mode, name, sha, full in list_dir(repo, root_tree, ""):
            if mode == "40000":
                entries.append({"type": "dir", "name": name, "path": full, "sha": sha})
            else:
                entries.append(file_entry_json(repo, full, mode, sha))
        return entries
    raise ApiError(404, "Not Found")


def commits_list(repo, ref):
    tip = resolve_ref_sha(repo, ref)
    if tip is None:
        return []
    commit_sha = repo.peel_to_commit(tip)
    if commit_sha is None:
        return []
    dist = ancestors_with_dist(repo, commit_sha)
    ordered = sorted(dist.items(), key=lambda kv: kv[1])
    out = []
    for sha, _d in ordered:
        parsed = parse_commit(repo.get(sha)[1])
        out.append({
            "sha": sha,
            "node_id": global_id("Commit", sha[:16]),
            "commit": {
                "message": parsed["message"],
                "tree": {"sha": parsed["tree"]},
                "author": {"name": VIEWER_NAME, "email": VIEWER_EMAIL},
                "committer": {"name": VIEWER_NAME, "email": VIEWER_EMAIL},
            },
            "parents": [{"sha": p} for p in parsed["parents"]],
        })
    return out


# --------------------------------------------------------------------------
# REST router. Returns (status, json_obj_or_None, extra_headers).

def rest_route(method, segments, query, body):
    if segments == [] or segments == [""]:
        if method == "GET":
            return 200, {"current_user_url": "http://forge.local/api/v3/user",
                         "repository_url": "http://forge.local/api/v3/repos/{owner}/{repo}"}, {"X-OAuth-Scopes": "repo, user, admin:org"}
        raise ApiError(404, "Not Found")
    if segments == ["meta"]:
        if method == "GET":
            return 200, {"installed_version": SERVER_VERSION, "verifiable_password_authentication": False}
        raise ApiError(404, "Not Found")
    if segments == ["user"]:
        if method == "GET":
            return 200, user_json()
        raise ApiError(404, "Not Found")
    if segments == ["user", "repos"]:
        if method == "POST":
            if not isinstance(body, dict) or not body.get("name"):
                raise ApiError(422, "Validation Failed: name is required")
            name = body["name"]
            if name in STORE:
                raise ApiError(422, "Validation Failed: name already exists on this account")
            repo = Repo(name, body.get("private", False), body.get("description", ""))
            STORE[name] = repo
            return 201, repo_json(repo)
        if method == "GET":
            repos = sorted(STORE.values(), key=lambda r: r.id)
            page_items, page, per_page, has_next = paginate([repo_json(r) for r in repos], query)
            headers = {}
            if has_next:
                host = _CURRENT_HOST[0] or "127.0.0.1"
                headers["Link"] = '<http://%s/api/v3/user/repos?per_page=%d&page=%d>; rel="next"' % (host, per_page, page + 1)
            return 200, page_items, headers
        raise ApiError(404, "Not Found")
    if len(segments) >= 3 and segments[0] == "repos":
        owner, name = segments[1], segments[2]
        repo = find_repo(owner, name)
        if repo is None:
            raise ApiError(404, "Not Found")
        rest = segments[3:]
        return repo_route(repo, method, rest, query, body)
    raise ApiError(404, "Not Found")


_CURRENT_HOST = [""]


def repo_route(repo, method, rest, query, body):
    if rest == []:
        if method == "GET":
            return 200, repo_json(repo)
        if method == "PATCH":
            if isinstance(body, dict):
                if "description" in body:
                    repo.description = body["description"] or ""
                if "private" in body:
                    repo.private = bool(body["private"])
                if "name" in body and body["name"] and body["name"] != repo.name:
                    del STORE[repo.name]
                    repo.name = body["name"]
                    STORE[repo.name] = repo
            return 200, repo_json(repo)
        if method == "DELETE":
            del STORE[repo.name]
            return 204, None
        raise ApiError(404, "Not Found")

    if rest == ["branches"] and method == "GET":
        out = []
        for ref in sorted_refs(repo):
            if ref.startswith("refs/heads/"):
                out.append({"name": ref[len("refs/heads/"):], "commit": {"sha": repo.refs[ref]}, "protected": False})
        return 200, out
    if len(rest) >= 2 and rest[0] == "branches" and method == "GET":
        branch = "/".join(rest[1:])
        tip = repo.branch_tip(branch)
        if tip is None:
            raise ApiError(404, "Not Found")
        return 200, {"name": branch, "commit": {"sha": tip}, "protected": False}

    if rest == ["commits"] and method == "GET":
        return 200, commits_list(repo, query.get("sha", [None])[0])

    if rest == ["git", "refs"] and method == "POST":
        if not isinstance(body, dict) or not body.get("ref") or not body.get("sha"):
            raise ApiError(422, "Validation Failed: ref and sha are required")
        ref = body["ref"]
        sha = body["sha"]
        repo.refs[ref] = sha
        return 201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}
    if len(rest) >= 3 and rest[0] == "git" and rest[1] == "refs" and method == "GET":
        ref = "/".join(rest[2:])
        if not ref.startswith("refs/"):
            ref = "refs/" + ref
        sha = repo.refs.get(ref)
        if sha is None:
            raise ApiError(404, "Not Found")
        return 200, {"ref": ref, "object": {"sha": sha, "type": "commit"}}

    if len(rest) >= 2 and rest[0] == "contents":
        path = "/".join(rest[1:])
        return contents_route(repo, method, path, query, body)

    if rest == ["labels"]:
        if method == "POST":
            if not isinstance(body, dict) or not body.get("name"):
                raise ApiError(422, "Validation Failed: name is required")
            lname = body["name"]
            if lname in repo.labels:
                raise ApiError(422, "Validation Failed: label '%s' already exists" % lname)
            label = {"id": next_label_id(), "name": lname,
                     "color": (body.get("color") or "ededed").lstrip("#"),
                     "description": body.get("description")}
            repo.labels[lname] = label
            return 201, label_json(label)
        if method == "GET":
            return 200, [label_json(l) for l in repo.labels.values()]
        raise ApiError(404, "Not Found")

    if rest == ["issues"]:
        if method == "POST":
            if not isinstance(body, dict) or body.get("title") is None:
                raise ApiError(422, "Validation Failed: title is required")
            issue = create_issue(repo, body["title"], body.get("body"), body.get("labels"))
            return 201, issue_json(repo, issue)
        if method == "GET":
            state = query.get("state", ["open"])[0]
            sort = query.get("sort", ["created"])[0]
            direction = query.get("direction", ["desc"])[0]
            items = [i for i in repo.issues.values() if state == "all" or i["state"] == state]
            items.sort(key=lambda i: i["number"], reverse=(direction != "asc"))
            page_items, _p, _pp, _hn = paginate([issue_json(repo, i) for i in items], query)
            return 200, page_items
        raise ApiError(404, "Not Found")

    if len(rest) >= 2 and rest[0] == "issues":
        try:
            number = int(rest[1])
        except ValueError:
            raise ApiError(404, "Not Found")
        issue = repo.issues.get(number)
        if len(rest) == 2:
            if issue is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                return 200, issue_json(repo, issue)
            if method == "PATCH":
                if isinstance(body, dict):
                    if "state" in body and body["state"] in ("open", "closed"):
                        issue["state"] = body["state"]
                    if "title" in body:
                        issue["title"] = body["title"]
                    if "body" in body:
                        issue["body"] = body["body"]
                    if "labels" in body:
                        for lname in body["labels"]:
                            if lname not in repo.labels:
                                raise ApiError(422, "Validation Failed: label '%s' does not exist" % lname)
                        issue["labels"] = list(body["labels"])
                    issue["updated_epoch"] = tick()
                return 200, issue_json(repo, issue)
            raise ApiError(404, "Not Found")
        if len(rest) == 3 and rest[2] == "comments":
            if issue is None:
                raise ApiError(404, "Not Found")
            if method == "POST":
                if not isinstance(body, dict) or body.get("body") is None:
                    raise ApiError(422, "Validation Failed: body is required")
                comment = {"id": next_comment_id(), "body": body["body"], "created_epoch": tick()}
                issue["comments"].append(comment)
                return 201, comment_json(comment)
            if method == "GET":
                return 200, [comment_json(c) for c in issue["comments"]]
            raise ApiError(404, "Not Found")
        raise ApiError(404, "Not Found")

    if rest == ["pulls"]:
        if method == "POST":
            if not isinstance(body, dict):
                raise ApiError(422, "Validation Failed")
            pull = create_pull(repo, body.get("title", ""), body.get("body"), body.get("head", ""), body.get("base", ""))
            return 201, pull_json(repo, pull)
        if method == "GET":
            state = query.get("state", ["open"])[0]
            items = []
            for pull in repo.pulls.values():
                if state == "all":
                    items.append(pull)
                elif state == "closed" and pull["state"] == "closed":
                    items.append(pull)
                elif state == "open" and pull["state"] == "open":
                    items.append(pull)
            items.sort(key=lambda p: p["number"])
            page_items, _p, _pp, _hn = paginate([pull_json(repo, p) for p in items], query)
            return 200, page_items
        raise ApiError(404, "Not Found")

    if len(rest) >= 2 and rest[0] == "pulls":
        try:
            number = int(rest[1])
        except ValueError:
            raise ApiError(404, "Not Found")
        pull = repo.pulls.get(number)
        if len(rest) == 2:
            if pull is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                return 200, pull_json(repo, pull)
            if method == "PATCH":
                if isinstance(body, dict):
                    if "state" in body and body["state"] in ("open", "closed"):
                        pull["state"] = body["state"]
                    if "title" in body:
                        pull["title"] = body["title"]
                    if "body" in body:
                        pull["body"] = body["body"]
                    pull["updated_epoch"] = tick()
                return 200, pull_json(repo, pull)
            raise ApiError(404, "Not Found")
        if pull is None:
            raise ApiError(404, "Not Found")
        if len(rest) == 3 and rest[2] == "files" and method == "GET":
            return 200, pull_files(repo, pull)
        if len(rest) == 3 and rest[2] == "merge" and method == "PUT":
            method_name = "squash"
            if isinstance(body, dict) and body.get("merge_method"):
                method_name = body["merge_method"]
            sha = merge_pull(repo, pull, method_name)
            return 200, {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}
        raise ApiError(404, "Not Found")

    raise ApiError(404, "Not Found")


def contents_route(repo, method, path, query, body):
    if method == "GET":
        ref = query.get("ref", [None])[0]
        return 200, content_response(repo, path, ref)
    if method == "PUT":
        if not isinstance(body, dict) or body.get("content") is None:
            raise ApiError(422, "Validation Failed: content is required")
        try:
            raw = base64.b64decode(body["content"])
        except Exception:
            raise ApiError(422, "Validation Failed: content is not valid base64")
        branch = body.get("branch") or repo.default_branch
        message = body.get("message") or "Update %s" % path
        tip = repo.branch_tip(branch)
        existing_sha = None
        if tip is not None:
            commit_sha = repo.peel_to_commit(tip)
            flat = flatten_tree(repo, parse_commit(repo.get(commit_sha)[1])["tree"])
            if path in flat:
                existing_sha = flat[path][1]
        provided_sha = body.get("sha")
        if existing_sha is not None:
            if provided_sha is not None and provided_sha != existing_sha:
                raise ApiError(409, "Conflict: %s does not match the current sha" % path)
            status = 200
        else:
            status = 201
        blob_sha = repo.put("blob", raw)
        commit_sha = apply_file_change(repo, branch, path, blob_sha, message)
        content = {"name": path.split("/")[-1], "path": path, "sha": blob_sha,
                   "size": len(raw), "encoding": "base64",
                   "content": base64.b64encode(raw).decode(), "type": "file"}
        return status, {"content": content, "commit": {"sha": commit_sha, "message": message}}
    if method == "DELETE":
        if not isinstance(body, dict):
            raise ApiError(422, "Validation Failed")
        branch = body.get("branch") or repo.default_branch
        message = body.get("message") or "Delete %s" % path
        tip = repo.branch_tip(branch)
        if tip is None:
            raise ApiError(404, "Not Found")
        commit_sha0 = repo.peel_to_commit(tip)
        flat = flatten_tree(repo, parse_commit(repo.get(commit_sha0)[1])["tree"])
        if path not in flat:
            raise ApiError(404, "Not Found")
        if body.get("sha") is not None and body["sha"] != flat[path][1]:
            raise ApiError(409, "Conflict: sha does not match")
        commit_sha = apply_file_change(repo, branch, path, None, message)
        return 200, {"content": None, "commit": {"sha": commit_sha, "message": message}}
    raise ApiError(404, "Not Found")


# --------------------------------------------------------------------------
# GraphQL

GQL_SCHEMA_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "createdAt", "updatedAt", "url",
              "author", "labels", "comments", "closed"],
    "PullRequest": ["id", "number", "title", "body", "state", "merged", "mergeable",
                    "headRefName", "baseRefName", "files", "createdAt", "updatedAt", "url", "author"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
                   "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequests",
                   "pullRequest", "createdAt", "updatedAt", "url", "isPrivate"],
    "User": ["login", "id", "name", "url"],
    "IssueComment": ["id", "body", "author", "createdAt", "url"],
}

GQL_ENUMS = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
}


class GqlError(Exception):
    pass


def gql_tokenize(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("...", i):
            tokens.append(("spread", "..."))
            i += 3
            continue
        if ch in "{}()[]:=@$!&|":
            tokens.append(("punct", ch))
            i += 1
            continue
        if ch == '"':
            if text.startswith('"""', i):
                end = text.index('"""', i + 3)
                tokens.append(("string", text[i + 3:end]))
                i = end + 3
                continue
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}.get(esc, esc))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append(("string", "".join(buf)))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            tokens.append(("float", float(raw)) if ("." in raw or "e" in raw or "E" in raw) else ("int", int(raw)))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("name", text[i:j]))
            i = j
            continue
        raise GqlError("unexpected character %r" % ch)
    tokens.append(("eof", None))
    return tokens


class GqlParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect_punct(self, ch):
        tok = self.next()
        if tok != ("punct", ch):
            raise GqlError("expected %r, got %r" % (ch, tok))

    def skip_balanced(self):
        depth = 0
        while True:
            tok = self.next()
            if tok[0] == "eof":
                return
            if tok in (("punct", "("), ("punct", "["), ("punct", "{")):
                depth += 1
            elif tok in (("punct", ")"), ("punct", "]"), ("punct", "}")):
                depth -= 1
                if depth == 0:
                    return

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            tok = self.peek()
            if tok == ("punct", "{"):
                operations.append({"type": "query", "name": None, "selections": self.parse_selection_set()})
            elif tok == ("name", "query") or tok == ("name", "mutation") or tok == ("name", "subscription"):
                op_type = self.next()[1]
                name = None
                if self.peek()[0] == "name":
                    name = self.next()[1]
                if self.peek() == ("punct", "("):
                    self.skip_balanced()  # variable definitions
                # directives
                while self.peek() == ("punct", "@"):
                    self.next()
                    self.next()
                    if self.peek() == ("punct", "("):
                        self.skip_balanced()
                operations.append({"type": op_type, "name": name, "selections": self.parse_selection_set()})
            elif tok == ("name", "fragment"):
                self.next()
                frag_name = self.next()[1]
                self.next()  # 'on'
                type_cond = self.next()[1]
                while self.peek() == ("punct", "@"):
                    self.next()
                    self.next()
                    if self.peek() == ("punct", "("):
                        self.skip_balanced()
                fragments[frag_name] = {"type": type_cond, "selections": self.parse_selection_set()}
            else:
                raise GqlError("unexpected token %r" % (tok,))
        return operations, fragments

    def parse_selection_set(self):
        self.expect_punct("{")
        selections = []
        while self.peek() != ("punct", "}"):
            selections.append(self.parse_selection())
        self.next()
        return selections

    def parse_selection(self):
        if self.peek()[0] == "spread":
            self.next()
            if self.peek() == ("name", "on"):
                self.next()
                type_cond = self.next()[1]
                return {"kind": "inline", "type": type_cond, "selections": self.parse_selection_set()}
            frag_name = self.next()[1]
            return {"kind": "spread", "name": frag_name}
        first = self.next()
        if first[0] != "name":
            raise GqlError("expected field name, got %r" % (first,))
        alias = None
        name = first[1]
        if self.peek() == ("punct", ":"):
            self.next()
            alias = name
            name = self.next()[1]
        args = {}
        if self.peek() == ("punct", "("):
            args = self.parse_arguments()
        while self.peek() == ("punct", "@"):
            self.next()
            self.next()
            if self.peek() == ("punct", "("):
                self.skip_balanced()
        selections = None
        if self.peek() == ("punct", "{"):
            selections = self.parse_selection_set()
        return {"kind": "field", "alias": alias, "name": name, "args": args, "selections": selections}

    def parse_arguments(self):
        self.expect_punct("(")
        args = {}
        while self.peek() != ("punct", ")"):
            key = self.next()[1]
            self.expect_punct(":")
            args[key] = self.parse_value()
        self.next()
        return args

    def parse_value(self):
        tok = self.next()
        if tok == ("punct", "$"):
            return ("var", self.next()[1])
        if tok[0] in ("string", "int", "float"):
            return ("lit", tok[1])
        if tok == ("punct", "["):
            items = []
            while self.peek() != ("punct", "]"):
                items.append(self.parse_value())
            self.next()
            return ("list", items)
        if tok == ("punct", "{"):
            obj = {}
            while self.peek() != ("punct", "}"):
                key = self.next()[1]
                self.expect_punct(":")
                obj[key] = self.parse_value()
            self.next()
            return ("object", obj)
        if tok[0] == "name":
            if tok[1] == "true":
                return ("lit", True)
            if tok[1] == "false":
                return ("lit", False)
            if tok[1] == "null":
                return ("lit", None)
            return ("enum", tok[1])
        raise GqlError("unexpected value token %r" % (tok,))


def gql_resolve_value(value, variables):
    kind = value[0]
    if kind == "var":
        return variables.get(value[1])
    if kind == "lit":
        return value[1]
    if kind == "enum":
        return value[1]
    if kind == "list":
        return [gql_resolve_value(v, variables) for v in value[1]]
    if kind == "object":
        return {k: gql_resolve_value(v, variables) for k, v in value[1].items()}
    return None


class GqlFieldError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def gql_type_of(obj):
    if isinstance(obj, dict):
        return obj.get("__type")
    return None


def gql_complete(obj, selections, ctx, errors, type_name):
    """Apply a selection set to a resolved object."""
    if obj is None:
        return None
    if isinstance(obj, list):
        return [gql_complete(item, selections, ctx, errors, type_name) for item in obj]
    if not isinstance(obj, dict) or "__type" not in obj:
        return obj
    out = {}
    actual_type = obj["__type"]
    for sel in selections or []:
        if sel["kind"] == "inline":
            if sel["type"] == actual_type:
                out.update(gql_complete(obj, sel["selections"], ctx, errors, actual_type))
            continue
        if sel["kind"] == "spread":
            frag = ctx["fragments"].get(sel["name"])
            if frag and frag["type"] == actual_type:
                out.update(gql_complete(obj, frag["selections"], ctx, errors, actual_type))
            continue
        key = sel["alias"] or sel["name"]
        args = {k: gql_resolve_value(v, ctx["variables"]) for k, v in sel["args"].items()}
        try:
            value = gql_resolve_field(actual_type, obj, sel["name"], args, ctx)
        except GqlFieldError as exc:
            errors.append({"message": exc.message})
            out[key] = None
            continue
        out[key] = gql_complete(value, sel["selections"], ctx, errors, None)
    return out


def gql_wrap(type_name, payload):
    obj = {"__type": type_name}
    obj.update(payload)
    return obj


def issue_gql(repo, issue):
    return gql_wrap("Issue", {
        "id": global_id("Issue", issue["id"]),
        "number": issue["number"],
        "title": issue["title"],
        "state": issue["state"].upper(),
        "body": issue["body"] if issue["body"] is not None else "",
        "_repo": repo, "_issue": issue,
    })


def pull_gql(repo, pull):
    if pull["merged"]:
        state = "MERGED"
    elif pull["state"] == "closed":
        state = "CLOSED"
    else:
        state = "OPEN"
    return gql_wrap("PullRequest", {
        "id": global_id("PullRequest", pull["id"]),
        "number": pull["number"],
        "title": pull["title"],
        "body": pull["body"] if pull["body"] is not None else "",
        "state": state,
        "merged": pull["merged"],
        "headRefName": pull["head"],
        "baseRefName": pull["base"],
        "_repo": repo, "_pull": pull,
    })


def repo_gql(repo):
    return gql_wrap("Repository", {
        "id": global_id("Repository", repo.id),
        "databaseId": repo.id,
        "name": repo.name,
        "nameWithOwner": "%s/%s" % (repo.owner, repo.name),
        "_repo": repo,
    })


def gql_resolve_field(type_name, obj, field, args, ctx):
    if field == "__typename":
        return type_name
    if type_name == "Query":
        if field == "viewer":
            return gql_wrap("User", {"login": VIEWER_LOGIN})
        if field == "repository":
            repo = find_repo(args.get("owner") or "", args.get("name") or "")
            return repo_gql(repo) if repo else None
        if field == "node":
            return gql_node(args.get("id"))
        if field == "__type":
            return gql_type_object(args.get("name"))
        raise GqlFieldError("Field '%s' doesn't exist on type 'Query'" % field)
    if type_name == "Mutation":
        return gql_mutation(field, args)
    if type_name == "User":
        if field == "login":
            return obj["login"]
        if field == "id":
            return global_id("User", VIEWER_ID)
        if field == "name":
            return VIEWER_NAME
        raise GqlFieldError("Field '%s' doesn't exist on type 'User'" % field)
    if type_name == "Repository":
        repo = obj["_repo"]
        if field in ("id", "databaseId", "name", "nameWithOwner"):
            return obj[field]
        if field == "owner":
            return gql_wrap("User", {"login": repo.owner})
        if field == "defaultBranchRef":
            if repo.branch_tip(repo.default_branch) is None:
                return None
            return gql_wrap("Ref", {"name": repo.default_branch})
        if field == "viewerPermission":
            return "ADMIN"
        if field == "hasIssuesEnabled":
            return True
        if field == "isPrivate":
            return repo.private
        if field == "issue":
            issue = repo.issues.get(args.get("number"))
            return issue_gql(repo, issue) if issue else None
        if field == "issues":
            return gql_issues_connection(repo, args)
        if field == "pullRequest":
            pull = repo.pulls.get(args.get("number"))
            return pull_gql(repo, pull) if pull else None
        if field == "pullRequests":
            pulls = sorted(repo.pulls.values(), key=lambda p: p["number"])
            first = args.get("first")
            if first:
                pulls = pulls[:first]
            return gql_wrap("PullRequestConnection", {"nodes": [pull_gql(repo, p) for p in pulls]})
        raise GqlFieldError("Field '%s' doesn't exist on type 'Repository'" % field)
    if type_name == "Ref":
        if field == "name":
            return obj["name"]
        raise GqlFieldError("Field '%s' doesn't exist on type 'Ref'" % field)
    if type_name == "Issue":
        if field in ("id", "number", "title", "state", "body"):
            return obj[field]
        raise GqlFieldError("Field '%s' doesn't exist on type 'Issue'" % field)
    if type_name == "PullRequest":
        pull = obj["_pull"]
        repo = obj["_repo"]
        if field in ("id", "number", "title", "body", "state", "merged", "headRefName", "baseRefName"):
            return obj[field]
        if field == "mergeable":
            return "CONFLICTING" if pull_conflicts(repo, pull) else "MERGEABLE"
        if field == "files":
            files = pull_files(repo, pull)
            nodes = [gql_wrap("PullRequestChangedFile", {"path": f["filename"]}) for f in files]
            first = args.get("first")
            if first:
                nodes = nodes[:first]
            return gql_wrap("PullRequestChangedFileConnection", {"nodes": nodes})
        raise GqlFieldError("Field '%s' doesn't exist on type 'PullRequest'" % field)
    if type_name == "PullRequestChangedFile":
        if field == "path":
            return obj["path"]
        raise GqlFieldError("Field '%s' doesn't exist on type 'PullRequestChangedFile'" % field)
    if type_name in ("IssueConnection", "PullRequestConnection", "PullRequestChangedFileConnection"):
        if field == "nodes":
            return obj["nodes"]
        if field == "pageInfo":
            return obj["pageInfo"]
        raise GqlFieldError("Field '%s' doesn't exist on type '%s'" % (field, type_name))
    if type_name == "PageInfo":
        if field in ("hasNextPage", "endCursor", "hasPreviousPage", "startCursor"):
            return obj.get(field)
        raise GqlFieldError("Field '%s' doesn't exist on type 'PageInfo'" % field)
    if type_name == "__Type":
        if field == "name":
            return obj["name"]
        if field == "fields":
            return [gql_wrap("__Field", {"name": n}) for n in obj["fields"]]
        if field == "enumValues":
            return [gql_wrap("__EnumValue", {"name": n}) for n in obj["enumValues"]]
        if field == "kind":
            return obj.get("kind", "OBJECT")
        raise GqlFieldError("Field '%s' doesn't exist on type '__Type'" % field)
    if type_name == "__Field":
        if field == "name":
            return obj["name"]
        raise GqlFieldError("Field '%s' doesn't exist on type '__Field'" % field)
    if type_name == "__EnumValue":
        if field == "name":
            return obj["name"]
        raise GqlFieldError("Field '%s' doesn't exist on type '__EnumValue'" % field)
    if type_name in ("CreateIssuePayload", "CloseIssuePayload"):
        if field == "issue":
            return obj["issue"]
        raise GqlFieldError("Field '%s' doesn't exist on type '%s'" % (field, type_name))
    if type_name in ("CreatePullRequestPayload", "MergePullRequestPayload"):
        if field == "pullRequest":
            return obj["pullRequest"]
        raise GqlFieldError("Field '%s' doesn't exist on type '%s'" % (field, type_name))
    if type_name == "AddCommentPayload":
        if field == "commentEdge":
            return obj["commentEdge"]
        raise GqlFieldError("Field '%s' doesn't exist on type 'AddCommentPayload'" % field)
    if type_name == "IssueCommentEdge":
        if field == "node":
            return obj["node"]
        raise GqlFieldError("Field '%s' doesn't exist on type 'IssueCommentEdge'" % field)
    if type_name == "IssueComment":
        if field in ("id", "body"):
            return obj[field]
        raise GqlFieldError("Field '%s' doesn't exist on type 'IssueComment'" % field)
    raise GqlFieldError("Field '%s' doesn't exist on type '%s'" % (field, type_name))


def gql_type_object(name):
    if name in GQL_SCHEMA_FIELDS:
        return gql_wrap("__Type", {"name": name, "fields": GQL_SCHEMA_FIELDS[name], "enumValues": [], "kind": "OBJECT"})
    if name in GQL_ENUMS:
        return gql_wrap("__Type", {"name": name, "fields": [], "enumValues": GQL_ENUMS[name], "kind": "ENUM"})
    return None


def gql_node(node_id):
    type_name, rest = parse_global_id(node_id or "")
    if type_name == "Repository":
        try:
            repo = find_repo_by_id(int(rest))
        except ValueError:
            return None
        return repo_gql(repo) if repo else None
    if type_name == "Issue":
        for repo in STORE.values():
            for issue in repo.issues.values():
                if issue["id"] == int(rest):
                    return issue_gql(repo, issue)
        return None
    if type_name == "PullRequest":
        for repo in STORE.values():
            for pull in repo.pulls.values():
                if pull["id"] == int(rest):
                    return pull_gql(repo, pull)
        return None
    return None


def gql_issues_connection(repo, args):
    items = sorted(repo.issues.values(), key=lambda i: i["number"])
    states = args.get("states")
    if states:
        wanted = {s.upper() for s in states}
        items = [i for i in items if i["state"].upper() in wanted]
    order_by = args.get("orderBy") or {}
    direction = (order_by.get("direction") or "ASC").upper()
    if direction == "DESC":
        items = list(reversed(items))
    after = args.get("after")
    if after:
        _t, rest = parse_global_id(after)
        try:
            after_number = int(rest)
            numbers = [i["number"] for i in items]
            if after_number in numbers:
                items = items[numbers.index(after_number) + 1:]
        except (TypeError, ValueError):
            pass
    first = args.get("first")
    total = len(items)
    if first:
        items = items[:first]
    has_next = len(items) < total
    end_cursor = None
    if items:
        end_cursor = global_id("cursor", items[-1]["number"])
    page_info = gql_wrap("PageInfo", {"hasNextPage": has_next, "endCursor": end_cursor,
                                      "hasPreviousPage": bool(after), "startCursor": end_cursor})
    return gql_wrap("IssueConnection", {"nodes": [issue_gql(repo, i) for i in items], "pageInfo": page_info})


def gql_mutation(field, args):
    inp = args.get("input") or {}
    if field == "createIssue":
        _t, rest = parse_global_id(inp.get("repositoryId") or "")
        repo = find_repo_by_id(int(rest)) if rest else None
        if repo is None:
            raise GqlFieldError("Repository not found")
        issue = create_issue(repo, inp.get("title") or "", inp.get("body"), None)
        return gql_wrap("CreateIssuePayload", {"issue": issue_gql(repo, issue)})
    if field == "closeIssue":
        node = gql_node(inp.get("issueId"))
        if node is None or node["__type"] != "Issue":
            raise GqlFieldError("Issue not found")
        issue = node["_issue"]
        issue["state"] = "closed"
        issue["updated_epoch"] = tick()
        return gql_wrap("CloseIssuePayload", {"issue": issue_gql(node["_repo"], issue)})
    if field == "createPullRequest":
        _t, rest = parse_global_id(inp.get("repositoryId") or "")
        repo = find_repo_by_id(int(rest)) if rest else None
        if repo is None:
            raise GqlFieldError("Repository not found")
        try:
            pull = create_pull(repo, inp.get("title") or "", inp.get("body"),
                               inp.get("headRefName") or "", inp.get("baseRefName") or "")
        except ApiError as exc:
            raise GqlFieldError(exc.message)
        return gql_wrap("CreatePullRequestPayload", {"pullRequest": pull_gql(repo, pull)})
    if field == "mergePullRequest":
        node = gql_node(inp.get("pullRequestId"))
        if node is None or node["__type"] != "PullRequest":
            raise GqlFieldError("Pull request not found")
        repo = node["_repo"]
        pull = node["_pull"]
        try:
            merge_pull(repo, pull, "squash")
        except ApiError as exc:
            raise GqlFieldError(exc.message)
        return gql_wrap("MergePullRequestPayload", {"pullRequest": pull_gql(repo, pull)})
    if field == "addComment":
        node = gql_node(inp.get("subjectId"))
        if node is None or node["__type"] != "Issue":
            raise GqlFieldError("Subject not found")
        issue = node["_issue"]
        comment = {"id": next_comment_id(), "body": inp.get("body") or "", "created_epoch": tick()}
        issue["comments"].append(comment)
        comment_obj = gql_wrap("IssueComment", {"id": global_id("IssueComment", comment["id"]), "body": comment["body"]})
        return gql_wrap("AddCommentPayload", {"commentEdge": gql_wrap("IssueCommentEdge", {"node": comment_obj})})
    raise GqlFieldError("Field '%s' doesn't exist on type 'Mutation'" % field)


def graphql_execute(document, variables, operation_name=None):
    tokens = gql_tokenize(document)
    operations, fragments = GqlParser(tokens).parse_document()
    if not operations:
        raise GqlError("no operation in document")
    operation = operations[0]
    if operation_name:
        for op in operations:
            if op["name"] == operation_name:
                operation = op
                break
    root_type = "Mutation" if operation["type"] == "mutation" else "Query"
    ctx = {"variables": variables or {}, "fragments": fragments}
    errors = []
    root = {"__type": root_type}
    data = gql_complete(root, operation["selections"], ctx, errors, root_type)
    result = {"data": data}
    if errors:
        result["errors"] = errors
    return result


# --------------------------------------------------------------------------
# MCP (legacy handshake era: initialize + tools/list + tools/call)

MCP_TOOLS = [
    {"name": "create_repo",
     "description": "Create a new repository for the authenticated user.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "Repository name"},
                                    "description": {"type": "string"},
                                    "private": {"type": "boolean"}},
                     "required": ["name"]}},
    {"name": "get_repo",
     "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                                    "title": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["owner", "repo", "title"]}},
    {"name": "list_issues",
     "description": "List issues in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                                    "state": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Create a pull request in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                                    "title": {"type": "string"}, "head": {"type": "string"},
                                    "base": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Merge a pull request (squash).",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                                    "pull_number": {"type": "integer"}},
                     "required": ["owner", "repo", "pull_number"]}},
]

MCP_TOOLS_BY_NAME = {tool["name"]: tool for tool in MCP_TOOLS}


def mcp_tool_result(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj)}],
            "structuredContent": obj, "isError": False}


def mcp_tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def mcp_call_tool(name, arguments):
    arguments = arguments or {}
    tool = MCP_TOOLS_BY_NAME.get(name)
    if tool is None:
        return None, (-32602, "Unknown tool: %s" % name)
    for required in tool["inputSchema"].get("required", []):
        if required not in arguments or arguments[required] is None:
            return None, (-32602, "Missing required argument: %s" % required)
    try:
        if name == "create_repo":
            repo_name = arguments["name"]
            if repo_name in STORE:
                return mcp_tool_result(repo_json(STORE[repo_name])), None
            repo = Repo(repo_name, arguments.get("private", False), arguments.get("description", ""))
            STORE[repo_name] = repo
            return mcp_tool_result(repo_json(repo)), None
        owner = arguments.get("owner") or VIEWER_LOGIN
        repo = find_repo(owner, arguments.get("repo") or "")
        if repo is None:
            return mcp_tool_error("Repository not found: %s/%s" % (owner, arguments.get("repo"))), None
        if name == "get_repo":
            return mcp_tool_result(repo_json(repo)), None
        if name == "create_issue":
            issue = create_issue(repo, arguments["title"], arguments.get("body"), None)
            return mcp_tool_result(issue_json(repo, issue)), None
        if name == "list_issues":
            state = arguments.get("state") or "open"
            items = [issue_json(repo, i) for i in sorted(repo.issues.values(), key=lambda i: i["number"])
                     if state == "all" or i["state"] == state]
            return mcp_tool_result(items), None
        if name == "create_pull_request":
            pull = create_pull(repo, arguments["title"], arguments.get("body"),
                               arguments["head"], arguments["base"])
            return mcp_tool_result(pull_json(repo, pull)), None
        if name == "merge_pull_request":
            pull = repo.pulls.get(int(arguments["pull_number"]))
            if pull is None:
                return mcp_tool_error("Pull request not found"), None
            merge_pull(repo, pull, "squash")
            return mcp_tool_result(pull_json(repo, pull)), None
    except ApiError as exc:
        return mcp_tool_error(exc.message), None
    return mcp_tool_error("Unsupported tool"), None


def mcp_handle(body_text):
    """Returns (http_status, response_obj_or_None)."""
    try:
        request = json.loads(body_text)
    except Exception:
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}
    if isinstance(request, list):
        responses = []
        for item in request:
            status, obj = mcp_handle_one(item)
            if obj is not None:
                responses.append(obj)
        return 200, responses
    return mcp_handle_one(request)


def mcp_handle_one(request):
    if not isinstance(request, dict):
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}
    if method == "server/discover":
        return 200, {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32601, "message": "Method not found: server/discover"}}
    if method == "initialize":
        offered = params.get("protocolVersion")
        revision = offered if offered in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05") else "2025-11-25"
        return 200, {"jsonrpc": "2.0", "id": req_id,
                     "result": {"protocolVersion": revision,
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": FORGE_NAME, "version": SERVER_VERSION}}}
    if method is not None and method.startswith("notifications/"):
        return 202, None
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        result, error = mcp_call_tool(params.get("name") or "", params.get("arguments") or {})
        if error is not None:
            return 200, {"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": error[0], "message": error[1]}}
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": result}
    return 200, {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32601, "message": "Method not found: %s" % method}}


# --------------------------------------------------------------------------
# HTTP layer

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SelemeneForge/" + SERVER_VERSION

    def setup(self):
        super().setup()
        # Small request/response exchanges dominate the workloads; Nagle's
        # algorithm would otherwise add a ~40 ms delayed-ACK stall per call.
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        pass

    # -- plumbing ------------------------------------------------------
    def _send(self, status, body_bytes, content_type, extra_headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body_bytes and self.command != "HEAD":
            self.wfile.write(body_bytes)

    def _send_json(self, status, obj, extra_headers=None, api=True):
        headers = dict(extra_headers or {})
        if api:
            headers.setdefault("X-GitHub-Request-Id", next_request_id())
        payload = b"" if obj is None else json.dumps(obj).encode()
        self._send(status, payload, "application/json", headers)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _authorized(self):
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        token = header[len("Bearer "):]
        if not token:
            return False
        if EXPECTED_TOKEN:
            return token == EXPECTED_TOKEN
        return True

    # -- verbs ---------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # -- routing -------------------------------------------------------
    def _dispatch(self, method):
        try:
            # Always consume the request body first: answering (e.g. a 401)
            # with unread body bytes on the socket would corrupt the next
            # request on this keep-alive connection.
            self._body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""
            self._route(method)
        except ApiError as exc:
            self._send_json(exc.status, {"message": exc.message})
        except Exception as exc:  # never take the server down
            try:
                self._send_json(500, {"message": "Internal error: %s" % exc})
            except Exception:
                pass

    def _route(self, method):
        parsed = urlparse(self.path)
        raw_path = parsed.path
        query = parse_qs(parsed.query)
        _CURRENT_HOST[0] = self.headers.get("Host") or ""

        if raw_path == "/healthz":
            self._send_json(200, {"ok": True}, api=False)
            return

        if raw_path == "/mcp":
            if method != "POST":
                self._send_json(404, {"message": "Not Found"}, api=False)
                return
            if not self._authorized():
                self._send_json(401, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32001, "message": "Unauthorized"}}, api=False)
                return
            body_text = self._body.decode("utf-8", "replace")
            with _lock:
                status, obj = mcp_handle(body_text)
            if obj is None:
                self._send(status, b"", "application/json")
            else:
                self._send_json(status, obj, api=False)
            return

        if raw_path == "/api/graphql":
            if method != "POST":
                self._send_json(404, {"message": "Not Found"})
                return
            if not self._authorized():
                self._send_json(401, {"message": "Unauthorized"})
                return
            raw = self._body
            try:
                payload = json.loads(raw.decode()) if raw else {}
            except Exception:
                self._send_json(400, {"message": "Malformed JSON body"})
                return
            with _lock:
                if isinstance(payload, list):
                    results = []
                    for item in payload:
                        results.append(self._graphql_one(item))
                    self._send_json(200, results)
                else:
                    self._send_json(200, self._graphql_one(payload))
            return

        if raw_path == "/api/v3" or raw_path.startswith("/api/v3/"):
            if not self._authorized():
                self._send_json(401, {"message": "Requires authentication"})
                return
            segments = [unquote(s) for s in raw_path[len("/api/v3"):].split("/") if s != ""]
            body = None
            raw = self._body
            if raw:
                try:
                    body = json.loads(raw.decode())
                except Exception:
                    self._send_json(400, {"message": "Malformed JSON body"})
                    return
            with _lock:
                status, obj, *rest = rest_route(method, segments, query, body)
            extra = rest[0] if rest else {}
            self._send_json(status, obj, extra)
            return

        # git smart HTTP: /<owner>/<repo>.git/...
        git_match = self._git_route(method, raw_path, query)
        if git_match:
            return

        self._send_json(404, {"message": "Not Found"})

    def _graphql_one(self, payload):
        if not isinstance(payload, dict) or not payload.get("query"):
            return {"data": None, "errors": [{"message": "No query provided"}]}
        try:
            return graphql_execute(payload["query"], payload.get("variables") or {},
                                   payload.get("operationName"))
        except GqlError as exc:
            return {"data": None, "errors": [{"message": str(exc)}]}
        except Exception as exc:
            return {"data": None, "errors": [{"message": "Internal: %s" % exc}]}

    def _git_route(self, method, raw_path, query):
        parts = [unquote(p) for p in raw_path.split("/") if p != ""]
        if len(parts) < 3 or not parts[1].endswith(".git"):
            return False
        owner = parts[0]
        repo_name = parts[1][:-len(".git")]
        tail = parts[2:]
        repo = find_repo_by_name(repo_name)
        if tail == ["info", "refs"] and method == "GET":
            if not self._authorized():
                self._send_json(401, {"message": "Requires authentication"})
                return True
            service = query.get("service", [""])[0]
            if repo is None:
                self._send_json(404, {"message": "Not Found"})
                return True
            version2 = (self.headers.get("Git-Protocol") or "") == "version=2"
            with _lock:
                if service == "git-upload-pack":
                    payload = advertise_upload_pack(repo, version2)
                    ctype = "application/x-git-upload-pack-advertisement"
                elif service == "git-receive-pack":
                    payload = advertise_receive_pack(repo)
                    ctype = "application/x-git-receive-pack-advertisement"
                else:
                    self._send_json(400, {"message": "Unknown service"})
                    return True
            self._send(200, payload, ctype)
            return True
        if tail == ["git-upload-pack"] and method == "POST":
            if not self._authorized():
                self._send_json(401, {"message": "Requires authentication"})
                return True
            if repo is None:
                self._send_json(404, {"message": "Not Found"})
                return True
            body = self._body
            version2 = (self.headers.get("Git-Protocol") or "") == "version=2"
            try:
                with _lock:
                    payload = handle_upload_pack(repo, body, version2)
            except Exception:
                if os.environ.get("FORGE_DEBUG"):
                    import traceback
                    traceback.print_exc()
                self._send_json(400, {"message": "Bad git request"})
                return True
            self._send(200, payload, "application/x-git-upload-pack-result")
            return True
        if tail == ["git-receive-pack"] and method == "POST":
            if not self._authorized():
                self._send_json(401, {"message": "Requires authentication"})
                return True
            if repo is None:
                self._send_json(404, {"message": "Not Found"})
                return True
            body = self._body
            try:
                with _lock:
                    payload = handle_receive_pack(repo, body)
            except Exception:
                self._send_json(400, {"message": "Bad git request"})
                return True
            self._send(200, payload, "application/x-git-receive-pack-result")
            return True
        return False


# --------------------------------------------------------------------------
# lifecycle

class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    # The clone workload opens 50 connections at once; the default backlog
    # of 5 drops SYNs and costs seconds of client retry backoff.
    request_queue_size = 512


def serve_forever(port):
    server = ForgeServer(("0.0.0.0", port), Handler)
    return server


def main():
    env_url = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
    if not env_url:
        port = int(os.environ.get("FORGE_PORT") or 0)
        server = serve_forever(port)
        actual = server.server_address[1]
        sys.stdout.write("PORT %d\n" % actual)
        sys.stdout.write("forge listening on http://127.0.0.1:%d\n" % actual)
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    server = serve_forever(0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request_json(url, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")

    # register with retries
    delay = 0.25
    while True:
        try:
            request_json(env_url + "/register", {"port": port})
            break
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    # poll done
    delay = 0.25
    while True:
        try:
            body = request_json(env_url + "/done")
            if body.get("done") is True:
                break
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 2.0)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
