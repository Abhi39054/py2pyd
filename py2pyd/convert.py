"""
Convert Python files/packages to binary extension modules (.pyd/.so) using Cython.

This module provides functionality to convert Python source files into compiled
binary extensions for performance improvement and code obfuscation.

Features:
- Convert single files or entire packages
- Automatic compiler detection (MSVC/MinGW on Windows, GCC on Linux/Mac)
- Comprehensive diagnostics and error reporting
- Cleanup of intermediate build files from source directory
- Cross-platform support (.pyd on Windows, .so on Linux/Mac)

Usage (programmatic):
    from py2pyd.convert import convert
    convert("path/to/module.py", output_dir="dist", cleanup=True)

Usage (CLI):
    python -m py2pyd.convert input.py --output dist --cleanup

Dependencies:
- Cython: For generating C code from Python
- C Compiler: MSVC on Windows, GCC on Linux/Mac
"""

from __future__ import annotations
import os
import sys
import sysconfig
import shutil
import tempfile
import logging
import subprocess
import platform
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from distutils.errors import DistutilsPlatformError


# =============================================================================
# CORE UTILITY FUNCTIONS
# =============================================================================

def _ensure_cython():
    """
    Ensure Cython is available and return the cythonize function.
    
    Returns:
        The cythonize function from Cython.Build
        
    Raises:
        RuntimeError: If Cython is not installed
    """
    try:
        import Cython  # noqa: F401
        from Cython.Build import cythonize  # type: ignore
        return cythonize
    except ImportError as exc:
        raise RuntimeError(
            "Cython is required to convert .py to .pyd/.so. "
            "Install with: pip install cython"
        ) from exc


def _get_python_library_info() -> Dict[str, str]:
    """
    Get Python library paths and names for linking.
    
    Returns:
        Dictionary containing:
        - include_dir: Python include directory path
        - library_dir: Python library directory path  
        - library_name: Python library filename
        - base_path: Base Python installation path
        
    Note:
        Handles virtual environments by locating the base Python installation.
    """
    # Get the actual Python installation path (not virtual environment)
    python_install_path = os.path.dirname(sys.executable)
    if 'venv' in python_install_path or '.venv' in python_install_path:
        python_install_path = sys.base_exec_prefix
    
    paths = {
        'include_dir': sysconfig.get_path('include'),
        'library_dir': os.path.join(python_install_path, 'libs'),
        'base_path': python_install_path,
    }
    
    # Determine library name based on Python version
    version = sys.version_info
    lib_name = f"python{version.major}{version.minor}"
    
    # On Windows, we need the .lib file
    if sys.platform.startswith("win"):
        lib_name += ".lib"
    
    paths['library_name'] = lib_name
    
    return paths


def _check_python_libraries() -> bool:
    """
    Verify that Python development libraries exist and are accessible.
    
    Returns:
        True if all required libraries are found, False otherwise
    """
    python_info = _get_python_library_info()
    
    # Check include directory
    if not os.path.exists(python_info['include_dir']):
        logging.error(f"Python include directory not found: {python_info['include_dir']}")
        return False
    
    # Check library directory
    if not os.path.exists(python_info['library_dir']):
        logging.error(f"Python library directory not found: {python_info['library_dir']}")
        return False
    
    # Check for library files
    lib_files = [f for f in os.listdir(python_info['library_dir']) if f.endswith('.lib')]
    if not lib_files:
        logging.error(f"No .lib files found in: {python_info['library_dir']}")
        return False
    
    expected_lib = python_info['library_name']
    if expected_lib not in lib_files:
        logging.warning(f"Expected library {expected_lib} not found. Found: {lib_files}")
        # Try to find any python library
        python_libs = [f for f in lib_files if f.startswith('python')]
        if python_libs:
            logging.info(f"Using alternative library: {python_libs[0]}")
            python_info['library_name'] = python_libs[0]
        else:
            logging.error("No Python libraries found!")
            return False
    
    logging.debug(f"Python include: {python_info['include_dir']}")
    logging.debug(f"Python libraries: {python_info['library_dir']}")
    logging.debug(f"Using library: {python_info['library_name']}")
    
    return True


# =============================================================================
# MODULE DISCOVERY
# =============================================================================

def _discover_modules(input_path: Path) -> List[Tuple[str, Path]]:
    """
    Discover Python modules to build from input path.
    
    Args:
        input_path: Path to .py file or package directory
        
    Returns:
        List of tuples (module_name, file_path) where module_name is the 
        dotted import name and file_path is the absolute path to the source file
        
    Raises:
        FileNotFoundError: If input path doesn't exist
        ValueError: If input file is not a .py file
    """
    if input_path.is_file():
        if input_path.suffix not in (".py",):
            raise ValueError("Input file must be a .py file")
        module_name = input_path.stem
        return [(module_name, input_path.resolve())]

    # Directory: treat as package if contains __init__.py
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")
        
    if (input_path / "__init__.py").exists():
        root = input_path.resolve()
        modules = []
        for p in root.rglob("*.py"):
            rel = p.relative_to(root)
            parts = [root.name] + list(rel.with_suffix("").parts)
            module_name = ".".join(parts)
            modules.append((module_name, p.resolve()))
        return modules

    # Directory but not a package: treat each .py as top-level module
    modules = []
    for p in input_path.rglob("*.py"):
        rel = p.relative_to(input_path)
        module_name = ".".join(rel.with_suffix("").parts)
        modules.append((module_name, p.resolve()))
    return modules


# =============================================================================
# COMPILER DETECTION AND CONFIGURATION
# =============================================================================

def _windows_gcc_compatibility() -> Tuple[bool, str]:
    """
    Check if GCC/MinGW is compatible with the current Python installation on Windows.
    
    Returns:
        Tuple of (compatible, reason) where:
        - compatible: True if GCC is compatible
        - reason: Description of compatibility status
    """
    if not sys.platform.startswith("win"):
        return False, "not windows"

    gcc = shutil.which("gcc")
    if not gcc:
        return False, "gcc not found on PATH"

    try:
        out = subprocess.check_output([gcc, "-dumpmachine"], stderr=subprocess.STDOUT)
        triple = out.decode().strip().lower()
    except Exception as exc:
        return False, f"failed to run gcc -dumpmachine: {exc}"

    # Determine architectures
    if any(k in triple for k in ("x86_64", "x64", "amd64")):
        gcc_arch = "x86_64"
    elif any(k in triple for k in ("i686", "i386", "i586")):
        gcc_arch = "i686"
    else:
        gcc_arch = "unknown"

    py_is_64 = sys.maxsize > 2**32
    py_arch = "x86_64" if py_is_64 else "i686"

    if gcc_arch == "unknown":
        return False, f"gcc target '{triple}' has unknown arch; cannot verify compatibility"

    if gcc_arch != py_arch:
        return False, f"architecture mismatch: gcc target '{triple}' ({gcc_arch}) != python ({py_arch})"

    if "mingw" not in triple:
        return False, f"gcc target '{triple}' does not look like mingw; using mingw toolchain is not recommended"

    return True, triple


def _diagnose_vs_build_tools() -> Dict[str, Any]:
    """
    Run comprehensive diagnostics for Visual Studio Build Tools installation.
    
    Returns:
        Dictionary with diagnostic information including:
        - build_tools_installed: Whether Build Tools are installed
        - vcvars_found: Whether vcvarsall.bat is found
        - cl_exe_found: Whether cl.exe is available
        - vcvars_paths: List of found vcvarsall.bat paths
        - suggested_commands: List of setup commands to run
    """
    info: Dict[str, Any] = {
        "build_tools_installed": False,
        "vcvars_found": False,
        "cl_exe_found": False,
        "vcvars_paths": [],
        "cl_path": None,
        "vs_installations": [],
        "suggested_commands": [],
        "python_arch": "x86_64" if sys.maxsize > 2**32 else "x86",
        "machine": platform.machine()
    }
    
    # Common Visual Studio installation paths
    search_paths = [
        r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools",
        r"C:\Program Files\Microsoft Visual Studio\2022\Community",
        r"C:\Program Files\Microsoft Visual Studio\2022\Professional",
        r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Enterprise",
    ]
    
    # Check each potential installation path
    for vs_path in search_paths:
        if os.path.exists(vs_path):
            info["vs_installations"].append(vs_path)
            info["build_tools_installed"] = True
            
            vcvars_path = os.path.join(vs_path, "VC", "Auxiliary", "Build", "vcvarsall.bat")
            if os.path.exists(vcvars_path):
                info["vcvars_found"] = True
                info["vcvars_paths"].append(vcvars_path)
                info["suggested_commands"].append(f'"{vcvars_path}" {info["python_arch"]}')
    
    # Check for cl.exe
    cl_path = shutil.which("cl")
    if cl_path:
        info["cl_exe_found"] = True
        info["cl_path"] = cl_path
    
    # Use vswhere for more comprehensive detection
    possible_vswhere = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), 
                    "Microsoft Visual Studio", "Installer", "vswhere.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), 
                    "Microsoft Visual Studio", "Installer", "vswhere.exe"),
        shutil.which("vswhere"),
    ]
    
    for vswhere_path in possible_vswhere:
        if vswhere_path and os.path.exists(vswhere_path):
            try:
                out = subprocess.check_output([
                    vswhere_path, 
                    "-latest", 
                    "-products", "*", 
                    "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property", "installationPath"
                ], stderr=subprocess.STDOUT, timeout=10)
                inst_path = out.decode().strip()
                if inst_path and inst_path not in info["vs_installations"]:
                    info["vs_installations"].append(inst_path)
                    vcvars_path = os.path.join(inst_path, "VC", "Auxiliary", "Build", "vcvarsall.bat")
                    if os.path.exists(vcvars_path) and vcvars_path not in info["vcvars_paths"]:
                        info["vcvars_found"] = True
                        info["vcvars_paths"].append(vcvars_path)
                        info["suggested_commands"].append(f'"{vcvars_path}" {info["python_arch"]}')
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                pass
    
    return info


def _setup_vs_environment() -> bool:
    """
    Attempt to automatically set up Visual Studio environment variables.
    
    Returns:
        True if setup was successful, False otherwise
    """
    try:
        diagnostics = _diagnose_vs_build_tools()
        
        if not diagnostics["vcvars_found"]:
            logging.warning("No vcvarsall.bat found for automatic environment setup")
            return False
        
        vcvars_path = diagnostics["vcvars_paths"][0]
        arch = "x64" if diagnostics["python_arch"] == "x86_64" else "x86"
        
        logging.info(f"Attempting to set up VS environment using: {vcvars_path}")
        logging.info(f"Target architecture: {arch}")
        
        # Create temporary batch file to capture environment
        with tempfile.NamedTemporaryFile(mode='w', suffix='.bat', delete=False, encoding='utf-8') as temp_bat:
            temp_bat_path = temp_bat.name
            temp_out_path = temp_bat_path + '_env.txt'
            
            batch_content = f"""@echo off
call "{vcvars_path}" {arch}
set > "{temp_out_path}"
"""
            temp_bat.write(batch_content)
        
        try:
            # Execute batch file and capture environment
            result = subprocess.run(
                [temp_bat_path], 
                capture_output=True, 
                text=True, 
                shell=True,
                timeout=30
            )
            
            if os.path.exists(temp_out_path):
                with open(temp_out_path, 'r', encoding='utf-8') as f:
                    env_lines = f.readlines()
                
                # Update current process environment
                for line in env_lines:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        var_name, var_value = line.split('=', 1)
                        os.environ[var_name] = var_value
                
                logging.info("Successfully configured Visual Studio environment")
                success = True
            else:
                logging.warning("Failed to capture VS environment variables")
                success = False
                
        finally:
            # Cleanup temporary files
            try:
                os.unlink(temp_bat_path)
                if os.path.exists(temp_out_path):
                    os.unlink(temp_out_path)
            except OSError:
                pass
        
        # Verify setup worked
        if success and shutil.which("cl"):
            logging.info("Visual Studio compiler (cl.exe) is now available")
            return True
        else:
            logging.warning("Automatic VS environment setup incomplete")
            return False
            
    except Exception as e:
        logging.warning(f"Automatic VS environment setup failed: {e}")
        return False


# =============================================================================
# CLEANUP FUNCTIONS
# =============================================================================

def _cleanup_intermediate_files(input_path: Path, keep_c_files: bool = False, 
                              keep_annotations: bool = False) -> None:
    """
    Clean up intermediate build files in the input directory.
    
    Args:
        input_path: Input path where source files are located
        keep_c_files: If True, keep generated C files
        keep_annotations: If True, keep annotation files
    """
    patterns_to_remove = []
    
    if not keep_c_files:
        patterns_to_remove.append("*.c")
        
    if not keep_annotations:
        patterns_to_remove.extend(["*.html", "*.css"])
    
    removed_files = []
    for pattern in patterns_to_remove:
        # Search in input directory and all subdirectories
        for file_path in input_path.rglob(pattern):
            try:
                if file_path.is_file():
                    file_path.unlink()
                    removed_files.append(file_path)
            except OSError as e:
                logging.debug(f"Could not remove {file_path}: {e}")
    
    if removed_files and logging.getLogger().isEnabledFor(logging.INFO):
        logging.info(f"Cleaned up {len(removed_files)} intermediate files from {input_path}")
        for file_path in removed_files[:5]:  # Show first 5
            logging.debug(f"  Removed: {file_path.relative_to(input_path)}")
        if len(removed_files) > 5:
            logging.debug(f"  ... and {len(removed_files) - 5} more")


def _cleanup_build_temp_files(build_temp_dir: Optional[Path] = None) -> None:
    """
    Clean up temporary build directory files.
    
    Args:
        build_temp_dir: Temporary build directory to clean
    """
    if not build_temp_dir or not build_temp_dir.exists():
        return
        
    patterns_to_remove = ["*.obj", "*.exp", "*.lib", "*.pdb", "*.res"]
    
    removed_files = []
    for pattern in patterns_to_remove:
        for file_path in build_temp_dir.rglob(pattern):
            try:
                if file_path.is_file():
                    file_path.unlink()
                    removed_files.append(file_path)
            except OSError as e:
                logging.debug(f"Could not remove {file_path}: {e}")
    
    if removed_files:
        logging.debug(f"Cleaned up {len(removed_files)} temporary build files")


# =============================================================================
# BUILD FUNCTION
# =============================================================================

def _build_extensions(
    modules: List[Tuple[str, Path]],
    output_dir: Path,
    language_level: int = 3,
    annotate: bool = False,
    extra_compile_args: Optional[List[str]] = None,
    extra_link_args: Optional[List[str]] = None,
    define_macros: Optional[List[Tuple[str, Optional[str]]]] = None,
    force: bool = False,
    use_mingw: bool = False,
    build_temp_dir: Optional[Path] = None,
) -> List[Path]:
    """
    Build extension modules for the given modules list.
    
    Args:
        modules: List of (module_name, source_path) tuples to build
        output_dir: Directory where built extensions will be placed
        language_level: Python language level (2 or 3)
        annotate: Generate HTML annotation files
        extra_compile_args: Additional compiler arguments
        extra_link_args: Additional linker arguments
        define_macros: C preprocessor macros to define
        force: Force rebuild even if sources are unchanged
        use_mingw: Use MinGW instead of MSVC on Windows
        build_temp_dir: Temporary directory for build artifacts
        
    Returns:
        List of paths to generated extension modules
        
    Raises:
        RuntimeError: If build fails or compiler is not available
    """
    cythonize = _ensure_cython()
    from setuptools import Extension
    from distutils.dist import Distribution

    # Validate Python development files
    if not _check_python_libraries():
        python_info = _get_python_library_info()
        raise RuntimeError(
            f"Python development files not found!\n\n"
            f"Required:\n"
            f"  Include: {python_info['include_dir']}\n"
            f"  Libraries: {python_info['library_dir']}\n\n"
            f"Solutions:\n"
            f"1. Reinstall Python and ensure development files are included\n"
            f"2. Install python.org version (not Microsoft Store)\n"
            f"3. Use --use-mingw to try MinGW build\n"
        )

    python_info = _get_python_library_info()
    
    # Prepare linker arguments
    final_link_args = list(extra_link_args or [])
    
    if sys.platform.startswith("win") and not use_mingw:
        # Add Python library directory
        if os.path.exists(python_info['library_dir']):
            final_link_args.append(f"/LIBPATH:{python_info['library_dir']}")
        else:
            raise RuntimeError(f"Python library directory not found: {python_info['library_dir']}")

    # Prepare extension modules
    ext_modules = []
    for mod_name, src in modules:
        ext = Extension(
            name=mod_name,
            sources=[str(src)],
            define_macros=define_macros or [],
            extra_compile_args=extra_compile_args or [],
            extra_link_args=final_link_args,
            include_dirs=[python_info['include_dir']],
            library_dirs=[python_info['library_dir']],
            libraries=[python_info['library_name'].replace('.lib', '')],
        )
        ext_modules.append(ext)

    # Configure Cython compiler directives
    compiler_directives = {
        "language_level": language_level,
        "embedsignature": True,  # Embed signatures for documentation
        "boundscheck": False,    # Disable bounds checking for performance
        "wraparound": False,     # Disable negative indexing wraparound
    }
    
    # Generate C code and build extensions
    cy_exts = cythonize(
        ext_modules,
        compiler_directives=compiler_directives,
        annotate=annotate,
        force=force,
        quiet=(logging.getLogger().getEffectiveLevel() > logging.INFO),
    )

    # Configure build distribution
    dist = Distribution({"name": "py2pyd_build", "ext_modules": cy_exts})
    cmd = dist.get_command_obj("build_ext")
    cmd.build_lib = str(output_dir)
    cmd.inplace = False
    
    # Set temporary build directory if specified
    if build_temp_dir:
        cmd.build_temp = str(build_temp_dir)

    # Windows compiler configuration
    try:
        if sys.platform.startswith("win"):
            if use_mingw:
                gcc_path = shutil.which("gcc")
                if gcc_path:
                    compatible, reason = _windows_gcc_compatibility()
                    if compatible:
                        logging.info(f"Using compatible MinGW compiler: {reason}")
                    else:
                        logging.warning(f"Using MinGW despite compatibility issues: {reason}")
                    
                    try:
                        cmd.compiler = "mingw32"
                        os.environ.setdefault("CC", "gcc")
                        os.environ.setdefault("CXX", "g++")
                    except Exception as e:
                        logging.debug(f"Could not set mingw32 compiler: {e}")
                else:
                    raise RuntimeError("MinGW requested but gcc not found on PATH")
            else:
                if not shutil.which("cl"):
                    logging.info("MSVC compiler (cl.exe) not found on PATH, attempting auto-setup...")
                    if not _setup_vs_environment():
                        diagnostics = _diagnose_vs_build_tools()
                        raise RuntimeError(
                            "Microsoft Visual C++ compiler (cl.exe) not found and auto-setup failed.\n\n"
                            "Please do one of the following:\n"
                            "1. Use 'x64 Native Tools Command Prompt for VS' instead of regular command prompt\n"
                            "2. Run the appropriate vcvarsall.bat command:\n" +
                            "\n".join(f"   {cmd}" for cmd in diagnostics["suggested_commands"]) + "\n"
                            "3. Install Visual Studio Build Tools with C++ workload\n"
                            "4. Use --use-mingw flag to try MinGW instead"
                        )
    except RuntimeError:
        raise
    except Exception as e:
        logging.debug(f"Compiler detection encountered issue: {e}")

    # Execute build
    cmd.ensure_finalized()
    try:
        dist.run_command("build_ext")
    except DistutilsPlatformError as exc:
        # Provide enhanced error information
        if sys.platform.startswith("win"):
            diagnostics = _diagnose_vs_build_tools()
            python_info = _get_python_library_info()
            
            error_lines = [
                "Building Python extensions on Windows requires proper configuration.",
                "",
                "DIAGNOSTICS:",
                f"- Python architecture: {diagnostics['python_arch']}",
                f"- cl.exe available: {diagnostics['cl_exe_found']}",
                f"- Python include: {python_info['include_dir']}",
                f"- Python library dir: {python_info['library_dir']}",
                "",
                "SOLUTIONS:",
                "1. Use 'x64 Native Tools Command Prompt for VS 2022'",
                "2. Ensure Python development files are installed",
                "3. Try: python -m py2pyd.convert input.py --use-mingw",
                "",
                "Original error: " + str(exc)
            ]
            
            raise RuntimeError("\n".join(error_lines)) from exc
        raise

    # Collect built artifacts
    results = []
    for ext in cy_exts:
        ext_filename = cmd.get_ext_filename(ext.name)
        path_parts = ext.name.split(".")
        out_path = Path(cmd.build_lib, *path_parts[:-1], os.path.basename(ext_filename))
        if out_path.exists():
            results.append(out_path.resolve())
            
    return results


# =============================================================================
# MAIN CONVERSION FUNCTION
# =============================================================================

def convert(
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
) -> List[str]:
    """
    Convert a .py file or package into binary extension modules (.pyd / .so).
    
    Args:
        input_path: Path to .py file or package directory
        output_dir: Output directory for built extensions (default: ./build_pyd)
        annotate: Generate HTML annotation files from Cython
        language_level: Python language level (2 or 3)
        extra_compile_args: Additional compiler arguments
        extra_link_args: Additional linker arguments  
        define_macros: C preprocessor macros to define
        force_rebuild: Force rebuild even if sources are unchanged
        use_mingw: Use MinGW instead of MSVC on Windows
        cleanup: Clean up intermediate build files from input directory
        keep_c_files: Keep generated C files (ignored if cleanup=False)
        build_temp_dir: Temporary directory for build artifacts
        
    Returns:
        List of paths to generated extension modules
        
    Raises:
        FileNotFoundError: If input path doesn't exist
        RuntimeError: If build fails or no modules found
    """
    input_p = Path(input_path)
    if not input_p.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    # Setup output directory
    out_dir = Path(output_dir) if output_dir else Path.cwd() / "build_pyd"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Setup temporary build directory
    temp_dir = Path(build_temp_dir) if build_temp_dir else None
    if temp_dir:
        temp_dir.mkdir(parents=True, exist_ok=True)

    # Discover modules to build
    modules = _discover_modules(input_p)
    if not modules:
        raise RuntimeError("No python modules found to compile")

    logging.info(f"Building {len(modules)} module(s) from {input_path}")
    
    # Windows-specific compiler setup
    if sys.platform.startswith("win") and not use_mingw:
        diagnostics = _diagnose_vs_build_tools()
        if not diagnostics["cl_exe_found"]:
            logging.info("MSVC compiler not detected, attempting auto-configuration...")
            if not _setup_vs_environment():
                logging.warning(
                    "MSVC compiler auto-configuration failed. "
                    "If build fails, try using 'x64 Native Tools Command Prompt for VS' "
                    "or use --use-mingw flag."
                )

    # Build extensions
    built = _build_extensions(
        modules=modules,
        output_dir=out_dir,
        language_level=language_level,
        annotate=annotate,
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
        define_macros=define_macros,
        force=force_rebuild,
        use_mingw=use_mingw,
        build_temp_dir=temp_dir,
    )

    # Cleanup intermediate files from INPUT directory
    if cleanup:
        logging.info("Cleaning up intermediate build files from source directory...")
        _cleanup_intermediate_files(
            input_p, 
            keep_c_files=keep_c_files,
            keep_annotations=annotate  # Keep annotations if they were requested
        )
        
        # Also clean up temporary build directory if specified
        if temp_dir:
            _cleanup_build_temp_files(temp_dir)

    # Return absolute paths to built artifacts
    return [str(p.resolve()) for p in built]


# =============================================================================
# DIAGNOSTICS AND CLI
# =============================================================================

def diagnose() -> Dict[str, Any]:
    """
    Run comprehensive build environment diagnostics.
    
    Returns:
        Dictionary with detailed diagnostic information
    """
    print("\n" + "="*70)
    print("PY2PYD BUILD ENVIRONMENT DIAGNOSTICS")
    print("="*70)
    
    diagnostics = _diagnose_vs_build_tools()
    python_info = _get_python_library_info()
    
    print(f"Python Version: {sys.version}")
    print(f"Platform: {sys.platform}")
    print(f"Architecture: {diagnostics['python_arch']} ({diagnostics['machine']})")
    
    print(f"\nPython Development Files:")
    print(f"  Executable: {sys.executable}")
    print(f"  Base Prefix: {sys.base_exec_prefix}")
    print(f"  Include: {python_info['include_dir']}")
    print(f"    Exists: {'✓' if os.path.exists(python_info['include_dir']) else '✗'}")
    print(f"  Library Dir: {python_info['library_dir']}")
    print(f"    Exists: {'✓' if os.path.exists(python_info['library_dir']) else '✗'}")
    
    # Check library files
    if os.path.exists(python_info['library_dir']):
        lib_files = [f for f in os.listdir(python_info['library_dir']) if f.endswith('.lib')]
        python_libs = [f for f in lib_files if f.startswith('python')]
        print(f"  Python Libraries: {len(python_libs)} found")
        for lib in python_libs:
            print(f"    - {lib}")
    else:
        print(f"  ❌ Library directory not found!")
    
    print(f"\nCompiler Tools:")
    print(f"  Build Tools Installed: {diagnostics['build_tools_installed']}")
    print(f"  cl.exe available: {diagnostics['cl_exe_found']}")
    if diagnostics['cl_path']:
        print(f"  cl.exe location: {diagnostics['cl_path']}")
    
    # Check Cython
    try:
        import Cython
        cython_ver = getattr(Cython, '__version__', 'unknown')
        print(f"\nCython: Version {cython_ver} - ✓")
    except ImportError:
        print(f"\nCython: ✗ NOT INSTALLED - Run: pip install cython")
    
    # MinGW check
    gcc_path = shutil.which("gcc")
    if gcc_path:
        compatible, reason = _windows_gcc_compatibility()
        status = "✓" if compatible else "⚠"
        print(f"\nMinGW: {status} Found at {gcc_path}")
        if not compatible:
            print(f"  Compatibility: {reason}")
    else:
        print(f"\nMinGW: ✗ Not found on PATH")
    
    print("\nRECOMMENDED ACTIONS:")
    recommendations = []
    
    if not os.path.exists(python_info['include_dir']) or not os.path.exists(python_info['library_dir']):
        recommendations.append("❌ Python development files missing - reinstall Python")
    
    if not diagnostics['cl_exe_found'] and sys.platform.startswith("win"):
        recommendations.append("❌ MSVC compiler not available")
        recommendations.append("   Use 'x64 Native Tools Command Prompt for VS 2022'")
    
    if not recommendations:
        recommendations.append("✓ Environment looks good - ready to build!")
    
    for rec in recommendations:
        print(f"  {rec}")
    
    print("="*70 + "\n")
    
    return {**diagnostics, **python_info}


def _cli(argv: Optional[List[str]] = None) -> int:
    """
    Command-line interface for py2pyd converter.
    
    Args:
        argv: Command line arguments (uses sys.argv if None)
        
    Returns:
        Exit code (0 for success, non-zero for error)
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert .py files/packages to binary extension modules (.pyd/.so) using Cython",
        epilog="""
Examples:
  python -m py2pyd.convert module.py --output dist
  python -m py2pyd.convert package/ --output dist --cleanup
  python -m py2pyd.convert --diagnose
        """
    )
    parser.add_argument("input", help="Path to .py file or package directory", nargs="?")
    parser.add_argument("--output", "-o", help="Output directory (default: ./build_pyd)", default=None)
    parser.add_argument("--annotate", action="store_true", help="Generate HTML annotation from Cython")
    parser.add_argument("--force", action="store_true", help="Force rebuild even if up-to-date")
    parser.add_argument("--use-mingw", action="store_true", help="Force use of mingw toolchain on Windows")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--diagnose", action="store_true", help="Run build environment diagnostics only")
    
    # Cleanup options
    cleanup_group = parser.add_argument_group("cleanup options")
    cleanup_group.add_argument("--cleanup", action="store_true", default=True, 
                              help="Clean up intermediate files from source directory after build (default: True)")
    cleanup_group.add_argument("--no-cleanup", action="store_false", dest="cleanup",
                              help="Keep intermediate files in source directory")
    cleanup_group.add_argument("--keep-c-files", action="store_true", 
                              help="Keep generated C files in source directory (only if --cleanup is used)")
    
    # Advanced options
    advanced_group = parser.add_argument_group("advanced options")
    advanced_group.add_argument("--extra-compile-args", help="Extra compile arguments (space-separated)")
    advanced_group.add_argument("--extra-link-args", help="Extra link arguments (space-separated)")
    advanced_group.add_argument("--build-temp-dir", help="Temporary build directory")
    advanced_group.add_argument("--language-level", type=int, choices=[2, 3], default=3,
                               help="Python language level (default: 3)")
    
    args = parser.parse_args(argv)

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(levelname)s: %(message)s' if log_level == logging.INFO else '%(asctime)s - %(levelname)s - %(message)s'
    )

    # Run diagnostics or show help
    if args.diagnose or not args.input:
        diagnose()
        if not args.input:
            print("\nUsage examples:")
            print("  python -m py2pyd.convert input.py --output dist")
            print("  python -m py2pyd.convert package/ --cleanup")
            print("  python -m py2pyd.convert --diagnose")
        return 0

    try:
        # Parse extra arguments
        extra_compile = args.extra_compile_args.split() if args.extra_compile_args else None
        extra_link = args.extra_link_args.split() if args.extra_link_args else None
        
        # Perform conversion
        artifacts = convert(
            input_path=args.input,
            output_dir=args.output,
            annotate=args.annotate,
            language_level=args.language_level,
            extra_compile_args=extra_compile,
            extra_link_args=extra_link,
            force_rebuild=args.force,
            use_mingw=args.use_mingw,
            cleanup=args.cleanup,
            keep_c_files=args.keep_c_files,
            build_temp_dir=args.build_temp_dir,
        )
        
        # Show results
        print("\n" + "="*50)
        print("✓ BUILD SUCCESSFUL!")
        print("="*50)
        print("Generated files:")
        for artifact in artifacts:
            print(f"  {artifact}")
        
        if args.cleanup and not args.keep_c_files:
            print("\nIntermediate files have been cleaned up from source directory.")
        elif args.cleanup and args.keep_c_files:
            print("\nIntermediate files cleaned up from source directory (C files kept).")
        else:
            print("\nIntermediate files preserved in source directory (use --cleanup to remove).")
            
        return 0
        
    except Exception as exc:
        logging.error("Build failed: %s", exc)
        print(f"\n💡 Troubleshooting tip: Run with --diagnose to check your environment", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())