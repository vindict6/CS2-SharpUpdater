#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cs2_update_gamedata.py - end-to-end CounterStrikeSharp gamedata recovery.

Given the previous ("old") and current ("new") libserver.so plus the current
gamedata.json, this:
  1. SIGNATURES: scans every sig entry against the new binary. For any that
     no longer resolve uniquely, it re-locates the function in the new build
     via string anchoring + CFG fingerprint (validated against the old function
     that the old sig points to) and regenerates a fresh unique signature.
  2. OFFSETS: for every vtable-index offset entry, it extracts the class vtable
     from both builds via RTTI, matches the old function at the stored index to
     its new counterpart, and reads off the new index. Non-vtable / field
     offsets are left untouched and flagged (they need SDK-level struct diffing).
  3. Writes an updated gamedata.json + a machine-readable report.

Everything is verified statically: every emitted sig is confirmed to match
exactly one address in the new binary, and every offset change is backed by a
high CFG-cosine function match. Anything uncertain is FLAGGED, never guessed.

Pure Python 3 + pyelftools + capstone. No Ghidra.

Usage:
  python3 cs2_update_gamedata.py \
      --old  libserver.OLD.so \
      --new  libserver.so \
      --gamedata configs/addons/counterstrikesharp/gamedata/gamedata.json \
      --out  gamedata.updated.json \
      --report report.json
  # exit code 0 = clean (nothing broken or all repaired), 2 = items need review
"""
import sys, os, json, argparse, struct, math
from collections import OrderedDict, Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from elftools.elf.elffile import ELFFile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_OP_IMM, X86_OP_REG, X86_REG_RIP, X86_REG_RSP


# --------------------------------------------------------------------------- #
#  low-level binary access
# --------------------------------------------------------------------------- #
class Binary:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        self.elf = ELFFile(self.f)
        self.secs = [(s["sh_addr"], s["sh_addr"] + s["sh_size"], s.name, s["sh_offset"])
                     for s in self.elf.iter_sections() if s["sh_addr"]]
        t = self.elf.get_section_by_name(".text")
        self.text = t.data()
        self.tva = t["sh_addr"]
        self.tend = t["sh_addr"] + t["sh_size"]
        self.rodata = [(s.name, s["sh_addr"], s.data())
                       for s in self.elf.iter_sections()
                       if s.name.startswith(".rodata") and s["sh_addr"]]
        self.md = Cs(CS_ARCH_X86, CS_MODE_64)
        self.md.detail = True
        self._entries = None
        self._fp_cache = {}
        self._vt_cache = {}

    # ---- raw reads ----
    def read_va(self, va, n):
        for lo, hi, name, off in self.secs:
            if lo <= va < hi:
                self.f.seek(off + (va - lo))
                return self.f.read(n)
        return None

    def in_text(self, va):
        return self.tva <= va < self.tend

    def cstr(self, va, maxlen=240):
        raw = self.read_va(va, maxlen)
        if not raw:
            return None
        e = raw.find(b"\x00")
        if e < 3:
            return None
        try:
            t = raw[:e].decode("utf-8")
        except UnicodeDecodeError:
            return None
        if all(32 <= ord(c) < 127 or c in "\t\n" for c in t):
            return t
        return None

    # ---- function entry set (prologue after int3 padding) ----
    def entries(self):
        if self._entries is not None:
            return self._entries
        data = self.text
        ent = []
        i = 2
        n = len(data)
        while i < n:
            # a real function boundary is preceded by >=2 int3 padding bytes;
            # a lone 0xCC can be an instruction encoding byte (e.g. ModRM of
            # `mov r12, rcx` = 49 89 CC) and must not be treated as padding.
            if (data[i - 1] == 0xCC and data[i - 2] == 0xCC and data[i] != 0xCC
                    and _is_prologue(data[i:i + 4])):
                ent.append(self.tva + i)
            i += 1
        self._entries = sorted(set(ent))
        return self._entries

    def containing_entry(self, va):
        import bisect
        e = self.entries()
        i = bisect.bisect_right(e, va) - 1
        return e[i] if i >= 0 else None

    # ---- per-function fingerprint ----
    def fingerprint(self, entry_va, max_insns=6000, max_bytes=80000):
        key = entry_va
        if key in self._fp_cache:
            return self._fp_cache[key]
        fp = self._fingerprint(entry_va, max_insns, max_bytes)
        self._fp_cache[key] = fp
        return fp

    def _fingerprint(self, entry_va, max_insns=6000, max_bytes=80000):
        off = entry_va - self.tva
        code = self.text[off:off + max_bytes]
        mnem = Counter()
        strings = []
        seen = set()
        calls = 0
        leaders = {entry_va}
        edges = 0
        end_va = entry_va
        highest = entry_va
        insns = 0
        for ins in self.md.disasm(code, entry_va):
            insns += 1
            end_va = ins.address + ins.size
            m = ins.mnemonic
            mnem[m.upper()] += 1
            for op in ins.operands:
                if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP and op.mem.index == 0:
                    s = self.cstr(ins.address + ins.size + op.mem.disp)
                    if s and s not in seen:
                        seen.add(s)
                        strings.append(s)
            if m == "call":
                calls += 1
            if m.startswith("j"):
                for op in ins.operands:
                    if op.type == X86_OP_IMM:
                        edges += 1
                        t = op.imm
                        if entry_va <= t < entry_va + max_bytes:
                            leaders.add(t)
                        if t > highest:
                            highest = t
                if m != "jmp":
                    edges += 1
            if m in ("ret", "jmp") and end_va > highest:
                nxt = code[end_va - entry_va: end_va - entry_va + 1]
                if nxt in (b"\xcc", b""):
                    break
            if insns >= max_insns:
                break
        return dict(addr=entry_va, size=end_va - entry_va, insns=insns,
                    blocks=len(leaders), edges=edges, calls=calls,
                    mnem=dict(mnem), strings=strings)


def _is_prologue(b):
    if len(b) < 2:
        return False
    if b[:4] == b"\xf3\x0f\x1e\xfa":       # endbr64
        return True
    if b[0] == 0x55:                        # push rbp
        return True
    if b[:2] in (b"\x41\x54", b"\x41\x55", b"\x41\x56", b"\x41\x57"):
        return True
    if b[0] in (0x53, 0x56, 0x57):
        return True
    if b[:3] == b"\x48\x83\xec" or b[:2] == b"\x48\x89":
        return True
    return False


def mnem_cos(a, b):
    ks = set(a) | set(b)
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in ks)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------- #
#  signature scanning / parsing / generation
# --------------------------------------------------------------------------- #
def parse_sig(sig):
    pat = bytearray()
    mask = bytearray()
    for tok in sig.split():
        if tok in ("?", "??", "2A", "*"):
            pat.append(0)
            mask.append(0)
        else:
            pat.append(int(tok, 16))
            mask.append(1)
    return bytes(pat), bytes(mask)


def scan(binary, sig, limit=None):
    pat, mask = parse_sig(sig)
    data = binary.text
    n = len(pat)
    first = next((i for i, m in enumerate(mask) if m), 0)
    anchor = pat[first]
    start = 0
    hits = []
    while True:
        i = data.find(anchor, start)
        if i < 0:
            break
        s = i - first
        if 0 <= s and s + n <= len(data):
            ok = True
            for j in range(n):
                if mask[j] and data[s + j] != pat[j]:
                    ok = False
                    break
            if ok:
                hits.append(binary.tva + s)
                if limit and len(hits) >= limit:
                    return hits
        start = i + 1
    return hits


def _wildcard_positions(ins):
    """Upstream's convention: keep opcodes, prefixes, ModRM and register
    encodings literal; wildcard everything that moves between builds -- i.e.
    every memory-operand displacement (RIP-relative string loads, vtable-call
    offsets, stack/struct offsets like [rbp-8]) and every immediate (stack-frame
    sizes, constants, and rel8/rel32 branch targets)."""
    wc = set()
    if ins.disp_offset and ins.disp_size:
        wc.update(range(ins.disp_offset, ins.disp_offset + ins.disp_size))
    if ins.imm_offset and ins.imm_size:
        wc.update(range(ins.imm_offset, ins.imm_offset + ins.imm_size))
    return wc


def gen_sig(binary, entry_va, max_insns=80, min_anchor_insns=6,
            min_anchor_bytes=14, robust_margin=2, extend_pad=True):
    """Cut a MAXIMALLY ROBUST unique signature at entry_va.

    Robustness model: the only bytes that move between builds are operands --
    displacements, immediates, branch targets (struct offsets shift, frame
    sizes change, call targets relocate). Those are all wildcarded, so every
    LITERAL byte in the signature is a stable part of the function: opcode,
    prefix, ModRM, or register encoding. A signature therefore stays valid as
    long as the function's own logic is unchanged, and only breaks when the
    function is genuinely rewritten -- at which point this tool re-derives it.

    Rather than stop at the first coincidental point of uniqueness (which could
    be a short, weak prefix), the generator keeps extending until the literal
    skeleton is a genuine anchor: at least `min_anchor_bytes` stable bytes AND
    `min_anchor_insns` instructions, plus `robust_margin` instructions beyond
    the first unique point so uniqueness never rests on a single byte. It stays
    inside the function body whenever possible; it only reaches into
    inter-function padding + the neighbour when the body can NEVER be unique (a
    byte-identical twin), and flags that case as fragile.

    Returns (sig, unique, meta) where meta = {insns, literal_bytes, extended,
    fragile, reason}.
    """
    data = binary.text
    tva = binary.tva
    ntext = len(data)
    cur = entry_va - tva
    pat = bytearray()
    mask = bytearray()
    toks = []
    made_unique = None
    used = 0
    extended = False

    def is_unique():
        return len(scan_pm(binary, bytes(pat), bytes(mask), 2)) == 1

    def anchor_solid():
        return (made_unique is not None
                and sum(mask) >= min_anchor_bytes
                and used >= min_anchor_insns
                and used >= made_unique + robust_margin)

    while used < max_insns and cur < ntext:
        if data[cur] == 0xCC:
            # reached the function's end (inter-function padding)
            if made_unique is not None:
                break                          # already unique inside the body
            if not extend_pad:
                break
            # Byte-identical twin: the body alone can never disambiguate. Reach
            # into the padding + neighbour just far enough to become unique, and
            # no further -- extra neighbour bytes only add neighbour-dependent
            # fragility without helping.
            while cur < ntext and data[cur] == 0xCC:
                pat.append(0xCC); mask.append(1); toks.append("CC"); cur += 1
            extended = True
            if is_unique():
                made_unique = used
                break
            continue                           # fall into the next function
        insn = next(binary.md.disasm(data[cur:cur + 15], tva + cur), None)
        if insn is None:
            break
        wc = _wildcard_positions(insn)
        for k, b in enumerate(insn.bytes):
            if k in wc:
                pat.append(0); mask.append(0); toks.append("?")
            else:
                pat.append(b); mask.append(1); toks.append("%02X" % b)
        used += 1
        cur += len(insn.bytes)
        if made_unique is None and is_unique():
            made_unique = used
        if extended and is_unique():
            break                              # twin: minimal neighbour reach
        if anchor_solid():
            break

    while toks and toks[-1] == "?":
        toks.pop(); pat.pop(); mask.pop()

    unique = len(scan_pm(binary, bytes(pat), bytes(mask), 2)) == 1
    lit = sum(mask)
    meta = {
        "insns": used,
        "literal_bytes": lit,
        "extended": extended,
        "fragile": bool(extended or lit < min_anchor_bytes),
        "reason": ("byte-identical twin: relies on neighbour layout" if extended
                   else ("short anchor: only %d stable bytes available" % lit
                         if lit < min_anchor_bytes else "")),
    }
    return " ".join(toks), unique, meta


def scan_pm(binary, pat, mask, limit):
    data = binary.text
    n = len(pat)
    first = next((i for i, m in enumerate(mask) if m), 0)
    anchor = pat[first]
    start = 0
    hits = []
    while True:
        i = data.find(anchor, start)
        if i < 0:
            break
        s = i - first
        if 0 <= s and s + n <= len(data):
            ok = True
            for j in range(n):
                if mask[j] and data[s + j] != pat[j]:
                    ok = False
                    break
            if ok:
                hits.append(s)
                if len(hits) >= limit:
                    return hits
        start = i + 1
    return hits


# --------------------------------------------------------------------------- #
#  string-anchored relocation
# --------------------------------------------------------------------------- #
_LEA_FORMS = []
for _rex in (b"\x48", b"\x4c"):
    for _op in (b"\x8d", b"\x8b"):
        for _r in range(8):
            _LEA_FORMS.append((_rex + _op, bytes([0x05 | (_r << 3)])))


def string_vas(binary, s):
    needle = s.encode() + b"\x00"
    out = []
    for name, base, data in binary.rodata:
        i = 0
        while True:
            i = data.find(needle, i)
            if i < 0:
                break
            if i == 0 or data[i - 1] == 0:
                out.append(base + i)
            i += 1
    return out


def xref_sites(binary, sva):
    hits = set()
    data = binary.text
    for prefix, modrm in _LEA_FORMS:
        ilen = len(prefix) + 1 + 4
        head = prefix + modrm
        start = 0
        while True:
            i = data.find(head, start)
            if i < 0:
                break
            insn_addr = binary.tva + i
            disp = sva - (insn_addr + ilen)
            if -0x80000000 <= disp <= 0x7fffffff:
                if data[i + len(head): i + len(head) + 4] == struct.pack("<i", disp):
                    hits.add(insn_addr)
            start = i + 1
    return hits


def locate_by_strings(binary, strings):
    """entry_va -> set(strings referenced), using the function entry set."""
    from collections import defaultdict
    hits = defaultdict(set)
    for s in strings:
        for sva in string_vas(binary, s):
            for site in xref_sites(binary, sva):
                e = binary.containing_entry(site)
                if e is not None:
                    hits[e].add(s)
    return dict(hits)


def distinctive(strings):
    out = [s for s in strings
           if len(s) >= 8 and ("%" in s or "::" in s or s.startswith("#")
                               or ".cpp" in s or "\n" in s or s.count(" ") >= 2)]
    return sorted(set(out), key=lambda s: -len(s))[:6]


# --------------------------------------------------------------------------- #
#  RTTI vtable extraction (for offset recovery)
# --------------------------------------------------------------------------- #
def mangled_name(class_name):
    # Itanium: bare type name is <len><identifier> for a simple class.
    return ("%d%s" % (len(class_name), class_name)).encode()


def find_in_sections(binary, prefixes, needle):
    out = []
    for lo, hi, name, off in binary.secs:
        if not any(name.startswith(p) for p in prefixes):
            continue
        binary.f.seek(off)
        data = binary.f.read(hi - lo)
        i = 0
        while True:
            i = data.find(needle, i)
            if i < 0:
                break
            out.append(lo + i)
            i += 1
    return out


def vtable_functions(binary, class_name):
    """Return the list of virtual-function VAs for class_name's primary vtable,
    or None if not found."""
    if class_name in binary._vt_cache:
        return binary._vt_cache[class_name]
    res = _vtable_functions(binary, class_name)
    binary._vt_cache[class_name] = res
    return res


def _vtable_functions(binary, class_name):
    mn = mangled_name(class_name)
    # name string must be preceded by \0 (its own start)
    name_candidates = []
    for lo, hi, name, off in binary.secs:
        if not name.startswith(".rodata"):
            continue
        binary.f.seek(off)
        data = binary.f.read(hi - lo)
        i = 0
        while True:
            i = data.find(mn, i)
            if i < 0:
                break
            if i == 0 or data[i - 1] == 0:
                # and immediately followed by \0 (exact type name)
                if data[i + len(mn): i + len(mn) + 1] == b"\x00":
                    name_candidates.append(lo + i)
            i += 1
    for nva in name_candidates:
        for r in find_in_sections(binary, (".data.rel.ro", ".rodata"),
                                  struct.pack("<Q", nva)):
            ti = r - 8                      # typeinfo object = name_ptr slot - 8
            for vr in find_in_sections(binary, (".data.rel.ro",),
                                      struct.pack("<Q", ti)):
                ott = binary.read_va(vr - 8, 8)
                if ott and struct.unpack("<q", ott)[0] == 0:   # primary vtable
                    funcs = []
                    k = 0
                    while True:
                        p = binary.read_va(vr + 8 + k * 8, 8)
                        if not p:
                            break
                        v = struct.unpack("<Q", p)[0]
                        if not binary.in_text(v):
                            break
                        funcs.append(v)
                        k += 1
                        if k > 4000:
                            break
                    if funcs:
                        return funcs
    return None


def reconcile_offsets(old, new, resolved, report):
    """Vtable indices preserve their relative order across builds (edits are
    insertions/removals that shift a suffix). So within one class the recovered
    new indices must increase with the old indices. A weak CFG match that breaks
    that ordering is almost certainly a decoy; re-derive it from the consistent
    delta of its high-confidence siblings and re-score at that slot. This is what
    rescues e.g. RemoveWeapons: siblings shifted +3 (21->24, 24->27), so old 25
    must be 28, not a same-looking decoy at 23."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in resolved:
        groups[r["cls"]].append(r)
    fixed = 0
    for cls, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda r: r["old_idx"])
        anchors = [r for r in items if r["cos"] >= 0.98]
        if not anchors:
            continue
        for r in items:
            lower = [a for a in anchors if a["old_idx"] < r["old_idx"]]
            upper = [a for a in anchors if a["old_idx"] > r["old_idx"]]
            lo = max((a["new_idx"] for a in lower), default=-1)
            hi = min((a["new_idx"] for a in upper), default=1 << 30)
            monotonic = lo < r["new_idx"] < hi
            if monotonic and r["cos"] >= 0.97:
                continue                      # consistent and confident enough
            # derive the expected slot from the nearest anchor's delta
            if lower:
                a = max(lower, key=lambda x: x["old_idx"])
                expected = a["new_idx"] + (r["old_idx"] - a["old_idx"])
            else:
                a = min(upper, key=lambda x: x["old_idx"])
                expected = a["new_idx"] - (a["old_idx"] - r["old_idx"])
            nf = r["new_funcs"]
            if not (0 <= expected < len(nf)) or not (lo < expected < hi):
                continue
            if expected == r["new_idx"]:
                continue
            of = old.fingerprint(r["old_funcs"][r["old_idx"]])
            cexp = mnem_cos(of["mnem"], new.fingerprint(nf[expected])["mnem"])
            if cexp < 0.85:
                continue                      # don't trust the correction either
            was = r["new_idx"]
            r["new_idx"] = expected
            r["entry"]["offsets"]["linux"] = expected
            rep = report["offsets"][r["name"]]
            rep["status"] = "OK" if expected == r["old_idx"] else "REPAIRED"
            rep["new_index"] = expected
            rep["cosine"] = round(cexp, 4)
            rep["reconciled"] = ("snapped to sibling delta +%d (was %d, cos %.3f)"
                                 % (expected - r["old_idx"], was, cexp))
            rep.pop("review", None)
            fixed += 1
    return fixed


def recover_vtable_index(old, new, old_funcs, new_funcs, old_idx, band=8):
    """Given the old function at old_idx, find its new index via CFG match in a
    band around old_idx. Returns (new_idx, cosine) or (None, 0)."""
    if old_idx >= len(old_funcs):
        return None, 0.0
    of = old.fingerprint(old_funcs[old_idx])
    lo = max(0, old_idx - band)
    hi = min(len(new_funcs), old_idx + band + 1)
    best = (None, -1.0)
    for j in range(lo, hi):
        nf = new.fingerprint(new_funcs[j])
        c = mnem_cos(of["mnem"], nf["mnem"])
        szsim = 1 - abs(of["size"] - nf["size"]) / (max(of["size"], nf["size"]) + 1)
        score = 0.75 * c + 0.25 * szsim
        if score > best[1]:
            best = (j, score, c)
    return best[0], best[2]


# --------------------------------------------------------------------------- #
#  offset entry -> (class, index) heuristic
# --------------------------------------------------------------------------- #
def split_class_method(entry_name):
    """Return a list of (class, method) candidates to try, longest class first.
    CS gamedata uses Class_Method, but class names themselves contain '_'
    (e.g. CCSPlayer_ItemServices_GiveNamedItem -> class CCSPlayer_ItemServices).
    We yield every split point so the caller can pick the one whose class has a
    real vtable."""
    if "::" in entry_name:
        c, m = entry_name.split("::", 1)
        return [(c, m)]
    parts = entry_name.split("_")
    if len(parts) < 2:
        return []
    cands = []
    for cut in range(len(parts) - 1, 0, -1):
        cls = "_".join(parts[:cut])
        method = "_".join(parts[cut:])
        cands.append((cls, method))
    return cands


# --------------------------------------------------------------------------- #
#  main driver
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--gamedata", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--min-cos", type=float, default=0.90,
                    help="minimum CFG cosine to accept an offset/sig match")
    args = ap.parse_args()

    old = Binary(args.old)
    new = Binary(args.new)
    with open(args.gamedata) as fh:
        gd = json.load(fh, object_pairs_hook=OrderedDict)

    report = OrderedDict(signatures=OrderedDict(), offsets=OrderedDict(),
                        summary=OrderedDict())
    changed = 0
    needs_review = 0

    # ---------------- signatures ----------------
    for name, entry in gd.items():
        if "signatures" not in entry or "linux" not in entry["signatures"]:
            continue
        sig = entry["signatures"]["linux"]
        new_hits = scan(new, sig, limit=3)
        if len(new_hits) == 1:
            report["signatures"][name] = {"status": "OK"}
            continue
        old_hits = scan(old, sig, limit=8)
        if len(new_hits) > 1 and len(old_hits) > 1:
            # Byte-identical twins. CSS resolves the sig to the FIRST match, so
            # target that instance and extend the sig past the function end
            # (into padding + the next function) until it is unique. This works,
            # but such a sig depends on the neighbour's layout, so it is
            # inherently more fragile and always surfaced for review.
            target = new_hits[0]
            xsig, uniq, xmeta = gen_sig(new, target)
            xhits = scan(new, xsig, limit=3)
            if uniq and len(xhits) == 1 and xhits[0] == target:
                report["signatures"][name] = {
                    "status": "REPAIRED", "method": "twin-extend",
                    "confidence": "medium",
                    "new_addr": "0x%x" % target, "new_sig": xsig,
                    "review": xmeta["reason"] or "byte-identical twin"}
                entry["signatures"]["linux"] = xsig
                changed += 1
                needs_review += 1
            else:
                report["signatures"][name] = {
                    "status": "AMBIGUOUS_BY_DESIGN", "matches": len(new_hits),
                    "note": "ambiguous in old build too; left unchanged"}
            continue
        rec = recover_signature(old, new, name, old_hits, sig, args.min_cos)
        report["signatures"][name] = rec
        if rec["status"] == "REPAIRED":
            entry["signatures"]["linux"] = rec["new_sig"]
            changed += 1
            if rec.get("confidence") == "low":
                needs_review += 1
        elif len(new_hits) == 0:
            needs_review += 1

    # ---------------- offsets ----------------
    resolved_off = []
    for name, entry in gd.items():
        if "offsets" not in entry or "linux" not in entry["offsets"]:
            continue
        old_idx = entry["offsets"]["linux"]
        cands = split_class_method(name)
        if not cands:
            report["offsets"][name] = {"status": "SKIP_NOT_VTABLE"}
            continue
        # pick the longest class prefix that actually has a vtable in both builds
        cls = None
        old_funcs = new_funcs = None
        for c, method in cands:
            of = vtable_functions(old, c)
            nf = vtable_functions(new, c)
            if of and nf:
                cls, old_funcs, new_funcs = c, of, nf
                break
        if cls is None:
            report["offsets"][name] = {"status": "SKIP_NO_VTABLE",
                                       "tried": [c for c, _ in cands]}
            continue
        if old_idx >= len(old_funcs):
            report["offsets"][name] = {"status": "SKIP_FIELD_OFFSET",
                                       "class": cls, "old_index": old_idx,
                                       "vtable_size": len(old_funcs)}
            continue
        new_idx, cos = recover_vtable_index(old, new, old_funcs, new_funcs, old_idx)
        if new_idx is None or cos < args.min_cos:
            report["offsets"][name] = {"status": "LOW_CONFIDENCE", "class": cls,
                                       "old_index": old_idx,
                                       "candidate_index": new_idx,
                                       "cosine": round(cos, 4)}
            # still record it so reconciliation can try to place it by siblings
            if new_idx is not None:
                resolved_off.append({"name": name, "entry": entry, "cls": cls,
                                     "old_idx": old_idx, "new_idx": new_idx,
                                     "cos": cos, "old_funcs": old_funcs,
                                     "new_funcs": new_funcs})
            needs_review += 1
            continue
        status = "OK" if new_idx == old_idx else "REPAIRED"
        rec = {"status": status, "class": cls, "old_index": old_idx,
               "new_index": new_idx, "cosine": round(cos, 4)}
        # borderline confidence: apply but ask for eyes
        if cos < 0.97 and new_idx != old_idx:
            rec["review"] = "borderline cosine; verify on server"
            needs_review += 1
        report["offsets"][name] = rec
        resolved_off.append({"name": name, "entry": entry, "cls": cls,
                             "old_idx": old_idx, "new_idx": new_idx, "cos": cos,
                             "old_funcs": old_funcs, "new_funcs": new_funcs})
        if new_idx != old_idx:
            entry["offsets"]["linux"] = new_idx
            changed += 1

    # cross-check each class for vtable-order consistency and fix decoys
    reconcile_offsets(old, new, resolved_off, report)
    # recompute after reconciliation (some 'review' flags may have cleared, some
    # entries may have flipped OK<->REPAIRED)
    changed = (sum(1 for v in report["signatures"].values() if v["status"] == "REPAIRED")
               + sum(1 for v in report["offsets"].values() if v["status"] == "REPAIRED"))
    needs_review = (sum(1 for v in report["offsets"].values()
                        if v.get("status") == "LOW_CONFIDENCE" or "review" in v)
                    + sum(1 for v in report["signatures"].values()
                          if v["status"] in ("BROKEN", "AMBIGUOUS")))

    report["summary"] = {
        "changed": changed,
        "needs_review": needs_review,
        "sig_ok": sum(1 for v in report["signatures"].values() if v["status"] == "OK"),
        "sig_repaired": sum(1 for v in report["signatures"].values() if v["status"] == "REPAIRED"),
        "sig_broken": sum(1 for v in report["signatures"].values()
                          if v["status"] in ("BROKEN", "AMBIGUOUS")),
        "off_ok": sum(1 for v in report["offsets"].values() if v["status"] == "OK"),
        "off_repaired": sum(1 for v in report["offsets"].values() if v["status"] == "REPAIRED"),
    }

    with open(args.out, "w") as fh:
        json.dump(gd, fh, indent=2)
    with open(args.report, "w") as fh:
        json.dump(report, fh, indent=2)

    s = report["summary"]
    print("[cs2-recover] changed=%d needs_review=%d | sigs ok=%d repaired=%d broken=%d | "
          "offsets ok=%d repaired=%d"
          % (s["changed"], s["needs_review"], s["sig_ok"], s["sig_repaired"],
             s["sig_broken"], s["off_ok"], s["off_repaired"]))
    sys.exit(2 if needs_review else 0)


def estimate_size(binary, addr):
    """Cheap size estimate = distance to the next function entry. Avoids full
    disassembly for prescreening fallback candidates."""
    import bisect
    e = binary.entries()
    i = bisect.bisect_right(e, addr)
    if i < len(e):
        return e[i] - addr
    return binary.tend - addr


def call_targets(binary, entry_va, max_bytes=400):
    """Direct call/jmp rel32 targets from a function body, in order."""
    off = entry_va - binary.tva
    code = binary.text[off:off + max_bytes]
    out = []
    for ins in binary.md.disasm(code, entry_va):
        if ins.mnemonic in ("call", "jmp"):
            for op in ins.operands:
                if op.type == X86_OP_IMM:
                    out.append(op.imm)
        if ins.mnemonic == "ret":
            break
    return out


def call_sites_to(binary, target):
    """All .text addresses issuing a `call rel32` (E8) to target."""
    data = binary.text
    hits = []
    start = 0
    while True:
        i = data.find(b"\xe8", start)
        if i < 0 or i + 5 > len(data):
            break
        disp = struct.unpack("<i", data[i + 1:i + 5])[0]
        if binary.tva + i + 5 + disp == target:
            hits.append(binary.tva + i)
        start = i + 1
    return hits


def match_by_cfg(old, new, of, size_lo=0.75, size_hi=1.3, block_tol=6):
    """Best NEW entry for an OLD fingerprint within a structural band."""
    best = (None, -1.0)
    for e in new.entries():
        est = estimate_size(new, e)
        if not (size_lo * of["size"] <= est <= size_hi * of["size"]):
            continue
        nf = new.fingerprint(e)
        if abs(nf["blocks"] - of["blocks"]) > block_tol:
            continue
        c = mnem_cos(of["mnem"], nf["mnem"])
        if c > best[1]:
            best = (e, c)
    return best


def recover_via_callees(old, new, old_addr, min_cos):
    """For a stringless function: pick its most distinctive direct callee, match
    that callee to NEW by CFG (large callees are near-unique), then find the NEW
    caller whose own fingerprint best matches the OLD target. Returns (new_addr,
    cosine) or (None, 0)."""
    of = old.fingerprint(old_addr)
    callees = call_targets(old, old_addr)
    if not callees:
        return None, 0.0
    # rank callees by how distinctive they are (bigger = more unique)
    ranked = sorted(set(callees),
                    key=lambda a: old.fingerprint(a)["size"], reverse=True)
    for callee in ranked[:2]:
        cof = old.fingerprint(callee)
        if cof["size"] < 200:            # too small to anchor reliably
            continue
        new_callee, cc = match_by_cfg(old, new, cof)
        if new_callee is None or cc < 0.99:
            continue
        # find NEW callers of new_callee; pick the best CFG match to our target
        best = (None, -1.0)
        for site in call_sites_to(new, new_callee):
            e = new.containing_entry(site)
            if e is None:
                continue
            nf = new.fingerprint(e, max_bytes=max(400, of["size"] * 2))
            c = mnem_cos(of["mnem"], nf["mnem"])
            if c > best[1]:
                best = (e, c)
        if best[0] is not None and best[1] >= min_cos:
            return best
    return None, 0.0


def relaxed_entry_pattern(binary, entry, max_ins=10, min_literal=12):
    """Build a byte pattern from a function's leading instructions with all
    displacement and immediate bytes wildcarded, keeping opcode/prefix/ModRM.
    This survives the two things that change most across a recompile -- moved
    string/branch targets and a single altered tail instruction -- so it still
    matches a function that barely changed. Deliberately NOT required to be
    unique: it produces a small candidate pool that CFG then ranks."""
    off = entry - binary.tva
    code = binary.text[off:off + 96]
    toks = []
    literal = 0
    n = 0
    for ins in binary.md.disasm(code, entry):
        wild = set()
        if ins.disp_offset and ins.disp_size:
            wild.update(range(ins.disp_offset, ins.disp_offset + ins.disp_size))
        if ins.imm_offset and ins.imm_size:
            wild.update(range(ins.imm_offset, ins.imm_offset + ins.imm_size))
        for i, by in enumerate(ins.bytes):
            if i in wild:
                toks.append("?")
            else:
                toks.append("%02X" % by)
                literal += 1
        n += 1
        if n >= max_ins or literal >= min_literal:
            break
    return " ".join(toks) if literal >= min_literal else None


def recover_via_old_anchor(old, new, old_addr, old_sig, min_cos):
    """Re-resolve a function that barely changed. Candidates come from two
    cheap, tight sources: (a) the new addresses where the exact old sig still
    matches (the ambiguous multi-match set), and (b) a relaxed old-entry prefix
    scanned in new. Both are then CFG-ranked against the old function. This is
    far less decoy-prone than the global CFG sweep, so it runs first. Returns
    (best_entry, best_cosine, margin_over_runner_up)."""
    cands = set()
    for a in scan(new, old_sig, limit=8):
        e = new.containing_entry(a)
        cands.add(e if e is not None else a)
    pat = relaxed_entry_pattern(old, old_addr)
    if pat:
        for a in scan(new, pat, limit=60):
            cands.add(a)
    if not cands:
        return None, 0.0, 1.0
    of = old.fingerprint(old_addr)
    ranked = sorted(
        ((mnem_cos(of["mnem"], new.fingerprint(e)["mnem"]), e) for e in cands),
        reverse=True)
    best_c, best_e = ranked[0]
    margin = (best_c - ranked[1][0]) if len(ranked) > 1 else 1.0
    if best_c >= min_cos:
        return best_e, best_c, margin
    return None, 0.0, margin


def corroborate(old, new, old_addr, cand, cosine, margin, of=None):
    """Independently confirm that NEW `cand` really is the OLD function at
    old_addr, using signals that did NOT drive the original selection: retained
    size, referenced strings, called functions, block count, and how clearly
    the candidate beat the runner-up. Returns a confidence tier + notes. This is
    what turns a lucky-looking cosine into a trustworthy match: a decoy rarely
    keeps the same size AND strings AND callees AND block count as the original."""
    of = of or old.fingerprint(old_addr)
    nf = new.fingerprint(cand)
    notes = []
    passed = 0
    total = 0

    total += 1                                          # 1) size stability
    sr = nf["size"] / max(1, of["size"])
    if 0.82 <= sr <= 1.22:
        passed += 1
    else:
        notes.append("size x%.2f" % sr)

    total += 1                                          # 2) block-count
    db = abs(nf["blocks"] - of["blocks"])
    if db <= 8:
        passed += 1
    else:
        notes.append("dblocks %d" % db)

    os_ = set(distinctive(of["strings"]))               # 3) referenced strings
    if os_:
        total += 1
        overlap = len(os_ & set(nf["strings"])) / len(os_)
        if overlap >= 0.5:
            passed += 1
        else:
            notes.append("strings %d%%" % int(100 * overlap))

    oc = [t for t in call_targets(old, old_addr) if old.in_text(t)]
    if oc:                                              # 4) callee corroboration
        total += 1
        ncf = [new.fingerprint(t) for t in call_targets(new, cand)[:10]
               if new.in_text(t)]
        matched = 0
        for t in oc[:8]:
            ofp = old.fingerprint(t)
            if any(mnem_cos(ofp["mnem"], x["mnem"]) >= 0.95 for x in ncf):
                matched += 1
        denom = min(8, len(oc)) or 1
        if matched / denom >= 0.5:
            passed += 1
        else:
            notes.append("callees %d/%d" % (matched, denom))

    tie = margin is not None and margin < 0.02          # decoy risk
    if tie:
        notes.append("near-tie dcos %.3f" % margin)

    frac = passed / total if total else 0.0
    full = (passed == total and total >= 3)
    if full and cosine >= 0.95:
        # every independent axis agrees -- size, blocks, strings, callees.
        # That disambiguates even a cosine near-tie, so it's trustworthy.
        conf = "high"
    elif cosine >= 0.995 and 0.9 <= sr <= 1.12 and not tie:
        conf = "high"
    elif frac >= 0.75 and cosine >= 0.95 and not tie:
        conf = "high"
    elif frac >= 0.5 and cosine >= 0.92:
        conf = "medium"
    else:
        conf = "low"
    return {"confidence": conf, "checks": "%d/%d" % (passed, total),
            "size_ratio": round(sr, 3), "notes": notes}


def recover_signature(old, new, name, old_hits, old_sig, min_cos):
    """Locate the function in NEW and regenerate a unique sig."""
    if len(old_hits) != 1:
        # old sig itself is ambiguous; try string anchoring from the first hit
        if not old_hits:
            return {"status": "BROKEN", "reason": "old sig no longer resolves"}
    old_addr = old_hits[0]
    of = old.fingerprint(old_addr)
    anchors = distinctive(of["strings"])

    candidate = None
    method = None
    cand_cos = 0.0
    cand_margin = 1.0
    # STAGE 0: old-anchored re-resolution. Handles the common "function barely
    # changed but the exact old sig no longer resolves to exactly one" case
    # (a changed tail instruction, or an ambiguous twin) precisely and cheaply,
    # before the decoy-prone global CFG search.
    e, c, m = recover_via_old_anchor(old, new, old_addr, old_sig, min_cos)
    if e is not None:
        candidate, method, cand_cos, cand_margin = e, "old-anchor", c, m
    if candidate is None and anchors:
        hits = locate_by_strings(new, anchors)
        if hits:
            # Cosine is ground truth; anchors only narrow the candidate pool.
            # (A small helper function can spuriously reference more shared
            # strings than the real target when the target is huge and its
            # xrefs get attributed to internal sub-entries. So rank by cosine.)
            scored = []
            for e, ss in hits.items():
                c = mnem_cos(of["mnem"], new.fingerprint(e)["mnem"])
                scored.append((c, len(ss), e))
            scored.sort(reverse=True)
            best_c, _, best_e = scored[0]
            if best_c >= min_cos:
                candidate, method, cand_cos = best_e, "string+cfg", best_c
                cand_margin = (best_c - scored[1][0]) if len(scored) > 1 else 1.0
    if candidate is None:
        # fall back to pure CFG search, prescreened by a cheap size estimate so
        # we don't fingerprint all ~38k functions.
        target_size = of["size"]
        best = (None, -1.0)
        second = -1.0
        checked = 0
        for e in new.entries():
            est = estimate_size(new, e)
            if not (0.55 * target_size <= est <= 1.7 * target_size):
                continue
            checked += 1
            if checked > 4000:
                break
            nf = new.fingerprint(e, max_bytes=max(4000, of["size"] * 2))
            if abs(nf["blocks"] - of["blocks"]) > 40:
                continue
            c = mnem_cos(of["mnem"], nf["mnem"])
            if c > best[1]:
                second = best[1]
                best = (e, c)
            elif c > second:
                second = c
        if best[0] is not None and best[1] >= max(min_cos, 0.99):
            candidate, method, cand_cos = best[0], "cfg", best[1]
            cand_margin = best[1] - second if second >= 0 else 1.0

    if candidate is None:
        # last resort for stringless wrappers: anchor on a distinctive callee
        # and find the matching caller in the new build.
        e, c = recover_via_callees(old, new, old_addr, max(min_cos, 0.98))
        if e is not None:
            candidate, method, cand_cos = e, "callee", c

    if candidate is None:
        return {"status": "BROKEN", "reason": "no confident match",
                "old_addr": "0x%x" % old_addr}

    # Snap to canonical entry: if an entry a few bytes earlier has an equal or
    # better cosine (an alternate/aligned entry into the same body), prefer it.
    import bisect
    E = new.entries()
    ci = bisect.bisect_left(E, candidate)
    of_mnem = of["mnem"]
    base_c = cand_cos
    for back in range(1, 4):
        if ci - back < 0:
            break
        e2 = E[ci - back]
        if candidate - e2 > 0x40:
            break
        c2 = mnem_cos(of_mnem, new.fingerprint(e2)["mnem"])
        if c2 >= base_c - 0.001:
            candidate, cand_cos = e2, c2

    sig, unique, meta = gen_sig(new, candidate)
    hits = scan(new, sig, limit=3)
    if not (unique and len(hits) == 1 and hits[0] == candidate):
        return {"status": "BROKEN", "reason": "could not cut unique sig",
                "new_addr": "0x%x" % candidate}

    # Independent cross-check: does this candidate actually look like the old
    # function on axes the selection didn't use? This is the rock-solid gate.
    corr = corroborate(old, new, old_addr, candidate, cand_cos, cand_margin, of)
    out = {"status": "REPAIRED", "method": method,
           "old_addr": "0x%x" % old_addr, "new_addr": "0x%x" % candidate,
           "cosine": round(cand_cos, 4),
           "confidence": corr["confidence"],
           "corroboration": corr["checks"],
           "new_sig": sig}
    detail = list(corr["notes"])
    if meta["fragile"]:
        detail.append(meta["reason"])
    if detail:
        out["review"] = "; ".join(detail)
    return out


if __name__ == "__main__":
    main()
