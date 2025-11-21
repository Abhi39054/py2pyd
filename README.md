# Py2Pyd — Python to PYD/.so Converter

[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20macos-lightgrey)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)

Py2Pyd converts Python modules and packages into compiled extension modules
(.pyd on Windows, .so on Linux/macOS) using Cython and the system C compiler.
It provides diagnostics for build tools, supports MSVC and MinGW on Windows,
and offers both CLI and programmatic APIs.

---

## Table of Contents
- [Key features](#key-features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Quick start](#quick-start)
- [CLI usage](#cli-usage)
- [Programmatic API](#programmatic-api)
- [Advanced options](#advanced-options)
- [Diagnostics & troubleshooting](#diagnostics--troubleshooting)
- [Best practices](#best-practices)
- [License](#license)

---

## Key features
- Convert single `.py` files or entire packages (recursively).
- Uses Cython to generate C and builds native extensions.
- Diagnostics for Visual Studio Build Tools, `cl.exe`, MinGW and Python dev files.
- Options: annotation (`--annotate`), force rebuild, cleanup of intermediate files.
- Cross-platform: supports Windows (MSVC / MinGW), Linux and macOS.

---

## Prerequisites
- Python 3.7+
- pip
- Cython: `pip install cython`
- **On Windows (Recommended)** [ ✅ Tested and Working]:
  - Microsoft Visual Studio Build Tools with "Desktop development with C++" workload
  - **Required Components**: MSVC v143, Windows 11 SDK
- **On Windows (Alternative)** [ ❌ Not Tested Yet]:
  - MinGW-w64 (must match Python bitness and be configured properly)
- **On Linux/macOS** [ ❌ Not Tested Yet]:
  - A working C compiler (gcc/clang) and Python development headers.
---

## Installation
Install locally (development) or from package index:

### From source:
```
  pip install -e .
```

### Ensure Cython:
```
  pip install cython setuptools
```

### Microsoft Visual Studio Build Tools Setup

#### Option 1: Using Visual Studio Installer (Recommended)

##### Step 1: Download
Get Visual Studio Build Tools from [Microsoft Official Site](https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022)

##### Step 2: Run Installer
Launch the Visual Studio Installer

##### Step 3: Select Workload
Choose **"Desktop development with C++"**

##### Step 4: Installation Details - Select These Components

```
  ✅ Desktop development with C++
  ✅ C++ Build Tools core features
  ✅ Visual C++ core desktop features  
  ✅ MSVC v143 - VS 2022 C++ x64/x86 build tools (Latest)
  ✅ Windows 11 SDK (10.0.26100.0 or later)
```

## Quick start

Tested method is that after installing `Microsoft Visual Studio Build Tools Setup`, press `Windows + s` Search `x64 Native Tools Command Prompt for VS` and Open it.

The go the project folder path and run below commands according to requiremnts.

Convert a single file:
```
  python -m py2pyd.convert path/to/module.py --output dist
```

Convert a package:
```
  python -m py2pyd.convert path/to/package_dir --output dist
```

Run diagnostics:
```
  python -m py2pyd.convert --diagnose
```


Important flags
- `--output, -o` : Output directory (default: `./build_pyd`)
- `--annotate` : Generate HTML annotation files
- `--force` : Force rebuild even if up-to-date
- `--use-mingw` : Force MinGW toolchain on Windows (risky; ensure compatibility)
- `--diagnose` : Run environment diagnostics and exit
- `--cleanup` / `--no-cleanup` : Clean intermediate files from source directory (default: cleanup)
- `--keep-c-files` : Keep generated `.c` files (when cleanup is used)
- `--extra-compile-args` : Extra compile args (space-separated)
- `--extra-link-args` : Extra link args (space-separated)
- `--build-temp-dir` : Temporary build directory
- `--language-level` : Cython language level (`2` or `3`, default `3`)
- `--verbose, -v` : Verbose logging

Examples:
- Single file with annotation:
```
  python -m py2pyd.convert my_module.py -o dist --annotate
```
- Package, keep C files:
```
  python -m py2pyd.convert my_package/ -o dist --no-cleanup --keep-c-files
```
- Force MinGW (if you understand the compatibility implications):
```
  python -m py2pyd.convert module.py --use-mingw
```

---

## Programmatic API

Function signature (from `py2pyd.convert`):
```
convert(
    input_path: str,
    output_dir: Optional[str] = None,
    annotate: bool = False,
    language_level: int = 3,
    extra_compile_args: Optional[List[str]] = None,
    extra_link_args: Optional[List[str]] = None,
    define_macros: Optional[List[Tuple[str, Optional[str]]]] = None,
    force_rebuild: bool = False,
    use_mingw: bool = False,
    cleanup: bool = True,
    keep_c_files: bool = False,
    build_temp_dir: Optional[str] = None,
) -> List[str]
```

Example:
```python
from py2pyd.convert import convert

artifacts = convert(
    "my_module.py",
    output_dir="dist",
    annotate=True,
    extra_compile_args=["/O2"],       # MSVC example; use "-O3" for GCC
    extra_link_args=None,
    cleanup=True,
)
print("Built:", artifacts)
```

There is also a `diagnose()` helper which prints and returns diagnostics:
```python
from py2pyd.convert import diagnose
diagnose()
```

---

## Advanced options
- Use `--extra-compile-args` / `--extra-link-args` to pass optimization flags.
- Use `--build-temp-dir` to control where temporary build artifacts are written.
- `--language-level` controls Cython's language level (2 or 3).
- On Windows, prefer running inside **x64 Native Tools Command Prompt** to ensure `cl.exe` and environment variables are set.

---

## Diagnostics & troubleshooting
Run:
```
  python -m py2pyd.convert --diagnose
```

Common problems and fixes:
- "cl.exe not found"
  - Open "x64 Native Tools Command Prompt for VS" or run `vcvarsall.bat`/`VsDevCmd.bat` for your VS installation.
- "Python development files not found" / missing `python3xx.lib`
  - Use Python distribution from python.org (not Microsoft Store) or reinstall with development headers.
- "Recognised but unhandled machine type" / linker errors with MinGW
  - Often an architecture mismatch (32-bit vs 64-bit) or incompatible MinGW variant. Use matching mingw-w64 or MSVC.
- If you encounter persistent issues, run `--diagnose` and include its output when seeking help.

Diagnostic quick-checks:
- Windows: `where cl`, `where gcc`
- Linux/macOS: `which gcc`
- Python include path: `python -c "import sysconfig; print(sysconfig.get_path('include'))"`

---

## Best practices
- Use virtual environments.
- Confirm code works under Python before compiling.
- Use Developer Command Prompt on Windows for MSVC builds.
- Keep source files for development; distribute compiled extensions for deployment.

---

## License
MIT — see the `LICENSE` file.

---

If you need more targeted examples (e.g., msys2/mingw-w64 setup or CI configuration), run `python -m py2pyd.convert --diagnose` and share the output for specific guidance.