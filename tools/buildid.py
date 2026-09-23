#!/usr/bin/env python3
"""Print the GNU Build ID of an ELF file (same value as `readelf -n`)."""
import sys

from elftools.elf.elffile import ELFFile

with open(sys.argv[1], "rb") as fh:
    for section in ELFFile(fh).iter_sections():
        if not section.name.startswith(".note"):
            continue
        for note in section.iter_notes():
            if note["n_type"] == "NT_GNU_BUILD_ID":
                print(note["n_desc"])
                sys.exit(0)
sys.exit(1)
