# Third-party source provenance

## foo2zjs / foo2xqx

Source: <https://github.com/OpenPrinting/foo2zjs>

Pinned commit: `80499ed5bf6caa2963ad337e37cfda78a80aab1e` (`main-fixes`).
Raw file URL template:
`https://raw.githubusercontent.com/OpenPrinting/foo2zjs/80499ed5bf6caa2963ad337e37cfda78a80aab1e/<path>`.

License: GNU GPL version 2 or later; preserved in `foo2zjs/COPYING` and source
headers. The original authors' notices remain in the source files.
`foo2zjs/COPYING` also lists copyright notices for vendor firmware and colour
profiles distributed by upstream foo2zjs; none of those files is included here.

Local changes (Windows binary I/O) in `foo2xqx.c`, `xqxdecode.c` and
`arm2hpdl.c`, each marked by a "MODIFIED FILE NOTICE" at the top of the file:

- include `windows_binary_io.h` and call `init_binary_stdio()` at the start of
  `main()`, switching stdin/stdout to binary mode on Windows;
- open binary files with `rb`, `wb` or `ab` instead of `r`, `w` or `a`.

This prevents Windows CRT newline and Ctrl-Z transformations. The encoder's
XQX/JBIG logic and the firmware wrapper format are unchanged. The changes were
made in October 2026 (local file timestamps 2026-10-21 UTC; there is no earlier
version-control history); the notices were added on 2026-11-16.
`windows_binary_io.h` is new code of this project (GPL-2.0-or-later).
MinGW-w64 supplies the existing `unistd.h` / `getopt` interface.

SHA-256 of the files at the pinned upstream commit (verified on 2026-11-16).
`COPYING` and `xqx.h` here are byte-identical to upstream; the three `.c` files
differ only by the changes above.

| File under `foo2zjs/` | Included | Upstream SHA-256 |
| --- | --- | --- |
| `COPYING` | yes, unmodified | `9c504edb1ae0bc25cf6191a06f1087c7a8d229b87df4f704fbcaa8415e28935f` |
| `xqx.h` | yes, unmodified | `4240c6ea82417cd0372f717504f41e297a3ba028d5e4cb3f78f7ac5b5ae34cb2` |
| `foo2xqx.c` | yes, modified | `95e8d78d14fb46c01e2e0b70184d12520cb65bd439e0d19e8a82736f3e781451` |
| `xqxdecode.c` | yes, modified | `f16355fae49eb63c710a88219d83d4f6a30f482cc1218f4221c2d51bdb9f5ae6` |
| `arm2hpdl.c` | yes, modified | `6b6b09477fc825c8207b6d7d67d1cf3f3cc0615f6c70fedf498f8132e89c5a50` |
| `foo2xqx-wrapper.in` | no, reviewed reference | `206d05e074954b5ce0626cfa0bd1bc81aceeda76eb85ead3921c4683eac80502` |
| `PPD/HP-LaserJet_P1006.ppd` | no, reviewed reference | `a22f213546f50a40a8f11433b10fc717a4e10650226014c703ca2b63684c8a32` |

`foo2xqx-wrapper.in` and the P1006 PPD were used as references for the
firmware upload sequence, page geometry and resolutions. They are not needed
by the build or the tests and are not redistributed; fetch them from the URLs
above if needed.

## JBIG-KIT 2.1

Author and project: Markus Kuhn, <https://www.cl.cam.ac.uk/~mgk25/jbigkit/>.

This checkout uses the copy bundled by <https://github.com/koenkooi/foo2zjs>,
pinned commit `e04290de6b7a30d588f3411fd9834618e09f7b9b`.
Raw file URL template:
`https://raw.githubusercontent.com/koenkooi/foo2zjs/e04290de6b7a30d588f3411fd9834618e09f7b9b/<path>`.

`jbig.h` identifies the included library as 2.1. License: GNU GPL version 2 or
later, as stated in the bundled source headers; `jbig/COPYING` is preserved.
No local modifications: all files are byte-identical to upstream.

| File under `jbig/` | Upstream SHA-256 |
| --- | --- |
| `COPYING` | `62b5d10a9370a11bf75b4a23cb06ca84a7df884e9f88f722fe1e47a562aeed89` |
| `jbig.c` | `2b1f7aa83461803cd588cf2e48bac4ba50e64e09cfb0da048c511b94c8c175db` |
| `jbig.h` | `0c016a8c69e349c510630edbf5b96057be952c2ac19c1a9b8488ad8c9864d7a4` |
| `jbig_ar.c` | `ae9fa2575d73a0cd50c6a32621ab2b6c4ca5f1115d27d636f461275c1d1e62f3` |
| `jbig_ar.h` | `292c4c43c944e2573b558d89b7004cec6c59c70dbbfd7b9890868a2291d1250a` |

JBIG-KIT 2.2 is available upstream and includes robustness fixes. The bundled
decoder is used here only as an offline diagnostic on our generated output;
it is not a parser for untrusted incoming print jobs.

## Build and verification

Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1`.
This process-local policy setting does not change system execution policy.
The default compiler is `C:\Strawberry\c\bin\gcc.exe` (MinGW-w64 from
Strawberry Perl). Use `-Compiler <path>` to select another MinGW-w64 GCC.

The default compiler emits x64 executables, not native ARM64 executables.
They run as ordinary user-mode programs using Windows 11 ARM64 application
emulation. They are not print-driver DLLs and do not install a kernel driver.
`bin/build-info.json` records compiler identity, target, source and binary hashes.

Run `python -m unittest discover -s tests -p test_native_converter.py -v` to
check binary pipe/file roundtrips, multi-page/chunked JBIG, default A4 crop,
and byte-for-byte firmware wrapping. These checks do not prove physical printing.

The P1006 PPD selects 1200x600 as its normal/default resolution. Its other
listed mode is special 600x400 draft, implemented with `-r1200x600 -R`.
The generic encoder and wrapper also accept 600x600; that path is tested
offline but device compatibility is unverified. Use 1200x600 by default.

HP firmware is proprietary and remains separate under `.vendor/`; it is not
part of the GPL source bundle. Extract it from the user's own HP package.
