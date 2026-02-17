"""
Allrun script whitelist validator for OpenFOAM simulation security.

Scans Allrun scripts for dangerous commands that could be injected via
prompt injection attacks. Provides both pre-execution blocking and
post-execution auditing capabilities.

Usage:
    from allrun_validator import validate_allrun, audit_allrun_scripts

    # Validate a single file
    result = validate_allrun("/path/to/Allrun")
    if not result['is_safe']:
        print("Dangerous commands found:", result['dangerous_commands'])

    # Audit all Allrun files in a job directory
    audit = audit_allrun_scripts("/path/to/runs/job_id/")
"""

import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Command classification ---

# High-danger commands that should NEVER appear in an Allrun script.
# Finding any of these triggers job failure (pre-check) or critical alert (audit).
DANGEROUS_COMMANDS = frozenset({
    # Filesystem destruction
    'rm', 'rmdir', 'shred',
    # Network access (data exfiltration / download)
    'curl', 'wget', 'nc', 'ncat', 'netcat',
    # Remote access
    'ssh', 'scp', 'rsync', 'sftp', 'ftp',
    # Arbitrary code execution
    'eval',
    # Raw disk / filesystem operations
    'dd', 'mkfs', 'fdisk', 'parted', 'mount', 'umount',
    # Privilege escalation
    'sudo', 'su',
    # Process management
    'kill', 'killall', 'pkill',
    # Permission / ownership changes
    'chmod', 'chown', 'chgrp',
    # User management
    'useradd', 'userdel', 'groupadd', 'passwd',
    # Firewall / networking
    'iptables', 'ip6tables', 'nft',
    # Service / system control
    'systemctl', 'service', 'reboot', 'shutdown', 'halt', 'poweroff',
    # Scheduled tasks
    'crontab', 'at',
    # Kernel modules
    'insmod', 'rmmod', 'modprobe',
})

# Allowed commands in Allrun scripts (OpenFOAM + basic shell)
ALLOWED_COMMANDS = frozenset({
    # --- OpenFOAM run functions (from RunFunctions) ---
    'runApplication', 'runParallel',
    'getApplication', 'getNumberOfProcessors',
    'compileApplication', 'cloneCase', 'restore0Dir',

    # --- OpenFOAM mesh generation ---
    'blockMesh', 'snappyHexMesh', 'extrudeMesh', 'refineMesh',
    'surfaceFeatures', 'surfaceFeatureExtract', 'surfaceFeatureConvert',

    # --- OpenFOAM mesh manipulation ---
    'checkMesh', 'topoSet', 'setFields', 'mapFields',
    'decomposePar', 'reconstructPar', 'reconstructParMesh',
    'createPatch', 'createBaffles', 'mergeMeshes', 'mirrorMesh',
    'renumberMesh', 'transformPoints', 'splitMeshRegions', 'flattenMesh',
    'autoPatch', 'subsetMesh',

    # --- OpenFOAM mesh converters ---
    'gmshToFoam', 'fluent3DMeshToFoam', 'fluentMeshToFoam',
    'ideasUnvToFoam', 'ccm26ToFoam', 'star4ToFoam',
    'plot3dToFoam', 'foamMeshToFluent', 'netgenNeutralToFoam',
    'kivaToFoam', 'ansysToFoam', 'cfx4ToFoam',

    # --- OpenFOAM solvers (common) ---
    'simpleFoam', 'icoFoam', 'pimpleFoam', 'pisoFoam', 'potentialFoam',
    'buoyantSimpleFoam', 'buoyantPimpleFoam',
    'buoyantBoussinesqSimpleFoam', 'buoyantBoussinesqPimpleFoam',
    'sonicFoam', 'sonicLiquidFoam', 'sonicDyMFoam',
    'rhoPimpleFoam', 'rhoSimpleFoam', 'rhoCentralFoam',
    'interFoam', 'interMixingFoam', 'multiphaseInterFoam',
    'compressibleInterFoam',
    'reactingFoam', 'reactingMultiphaseEulerFoam',
    'sprayFoam', 'XiFoam', 'fireFoam',
    'solidFoam', 'solidDisplacementFoam',
    'solidEquilibriumDisplacementFoam',
    'chtMultiRegionFoam', 'chtMultiRegionSimpleFoam',
    'SRFSimpleFoam', 'SRFPimpleFoam',
    'adjointOptimisationFoam', 'adjointShapeOptimizationFoam',
    'laplacianFoam', 'scalarTransportFoam',
    'driftFluxFoam', 'twoPhaseEulerFoam', 'multiphaseEulerFoam',
    'particleFoam', 'DPMFoam', 'MPPICFoam',
    'porousSimpleFoam', 'pimpleDyMFoam', 'interPhaseChangeFoam',
    'cavitatingFoam', 'compressibleMultiphaseInterFoam',
    'overSimpleFoam', 'overPimpleFoam', 'overInterDyMFoam',
    'electrostaticFoam', 'magneticFoam', 'mhdFoam',
    'dnsFoam', 'boundaryFoam',

    # --- OpenFOAM post-processing ---
    'foamToVTK', 'postProcess', 'foamToEnsight',
    'foamToGMV', 'foamToTecplot360',
    'sample', 'probeLocations',
    'foamCalc', 'foamLog',
    'paraFoam', 'pvpython', 'pvbatch',

    # --- OpenFOAM utilities ---
    'foamDictionary', 'foamFormatConvert', 'foamListTimes',
    'foamCleanTutorials', 'foamRunTutorials',
    'foamCleanCase', 'foamCloneCase',
    'foamGet', 'foamInfo',
    'createZeroDirectory',

    # --- Safe shell commands ---
    'cp', 'mv', 'ln', 'mkdir', 'cd', 'ls', 'cat', 'echo', 'printf',
    'touch', 'head', 'tail', 'grep', 'sed', 'awk', 'sort', 'wc', 'tr',
    'cut', 'find', 'xargs', 'tee', 'diff',
    'source', '.', 'exit', 'return',
    'test', '[', '[[',
    'set', 'unset', 'export', 'local', 'declare', 'typeset',
    'shift', 'wait', 'sleep',
    'true', 'false',
    'which', 'type', 'command',
    'basename', 'dirname', 'pwd', 'realpath', 'readlink',
    'date',

    # --- Shell control flow keywords ---
    'if', 'then', 'else', 'elif', 'fi',
    'for', 'in', 'do', 'done',
    'while', 'until',
    'case', 'esac',
    'function',
})

# Regex patterns that indicate dangerous shell execution tricks
DANGEROUS_PATTERNS = [
    re.compile(r'\bbash\s+-c\b'),          # bash -c "arbitrary command"
    re.compile(r'\bsh\s+-c\b'),            # sh -c "arbitrary command"
    re.compile(r'\bperl\s+-e\b'),          # perl -e "arbitrary code"
    re.compile(r'\bruby\s+-e\b'),          # ruby -e "arbitrary code"
    re.compile(r'\bpython[23]?\s+-c\b'),   # python -c "arbitrary code"
]

# Patterns that identify lines to skip (not actual commands)
SAFE_LINE_PATTERNS = [
    re.compile(r'^\s*#'),             # comments
    re.compile(r'^\s*$'),             # blank lines
    re.compile(r'^\s*\w[\w]*='),      # variable assignments (VAR=value)
    re.compile(r'^#!'),               # shebang
]


def _extract_commands_from_line(line):
    """
    Extract command names from a shell script line.

    Identifies tokens in command position:
    - First word of the line / each sub-statement
    - Words after pipe |, &&, ||, or ;
    - The actual command argument to runApplication/runParallel

    Returns:
        list of command name strings
    """
    # Strip inline comments (simplified: ignores # inside quotes)
    in_single = False
    in_double = False
    for i, c in enumerate(line):
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == '#' and not in_single and not in_double:
            line = line[:i]
            break

    commands = []

    # Split on shell operators to find separate statements
    parts = re.split(r'\s*(?:\|\||&&|[|;])\s*', line.strip())

    for part in parts:
        part = part.strip()
        if not part:
            continue

        tokens = part.split()
        if not tokens:
            continue

        first_token = tokens[0]

        # Strip leading path (e.g., ./Allrun.pre -> Allrun.pre)
        if '/' in first_token:
            first_token = first_token.rsplit('/', 1)[-1]

        commands.append(first_token)

        # If runApplication/runParallel, the next argument is the real command
        if first_token in ('runApplication', 'runParallel') and len(tokens) > 1:
            next_token = tokens[1]
            # Handle $(getApplication) or `getApplication`
            inner = (
                re.search(r'\$\((\w+)\)', next_token)
                or re.search(r'`(\w+)`', next_token)
            )
            if inner:
                commands.append(inner.group(1))
            elif next_token.startswith('$'):
                pass  # variable reference like $application — skip
            else:
                commands.append(next_token)

    return commands


def validate_allrun(file_path):
    """
    Validate an Allrun script against the command whitelist.

    Args:
        file_path: Path to the Allrun file.

    Returns:
        dict with keys:
            is_safe (bool): True if no dangerous commands found.
            dangerous_commands (list): Entries for dangerous commands found.
            unknown_commands (list): Non-whitelisted (but not dangerous) commands.
            scanned_lines (int): Number of meaningful lines scanned.
            file_path (str): The file that was scanned.
    """
    result = {
        'is_safe': True,
        'dangerous_commands': [],
        'unknown_commands': [],
        'scanned_lines': 0,
        'file_path': str(file_path),
    }

    try:
        with open(file_path, 'r') as f:
            lines = f.readlines()
    except (OSError, IOError) as e:
        logger.warning(f"Cannot read Allrun file {file_path}: {e}")
        result['error'] = str(e)
        return result

    for line_num, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()

        # Skip safe line patterns
        if any(pat.match(line) for pat in SAFE_LINE_PATTERNS):
            continue

        result['scanned_lines'] += 1

        # Check for dangerous shell execution patterns
        for pat in DANGEROUS_PATTERNS:
            if pat.search(line):
                result['is_safe'] = False
                result['dangerous_commands'].append({
                    'line_num': line_num,
                    'line': line,
                    'command': pat.pattern,
                    'reason': 'Matches dangerous execution pattern',
                })
                # Don't break — also check individual commands below

        # Extract and classify individual commands
        commands = _extract_commands_from_line(line)
        for cmd in commands:
            cmd_name = cmd.lstrip('./')
            if not cmd_name:
                continue

            if cmd_name in DANGEROUS_COMMANDS:
                result['is_safe'] = False
                result['dangerous_commands'].append({
                    'line_num': line_num,
                    'line': line,
                    'command': cmd_name,
                    'reason': 'Command is in dangerous list',
                })
            elif cmd_name not in ALLOWED_COMMANDS:
                result['unknown_commands'].append({
                    'line_num': line_num,
                    'line': line,
                    'command': cmd_name,
                })

    return result


def audit_allrun_scripts(run_dir):
    """
    Find and validate all Allrun scripts in a job's run directory.

    Args:
        run_dir: Path to the job's run directory (e.g., runs/{job_id}/).

    Returns:
        dict with keys:
            is_safe (bool): True if all Allrun files passed validation.
            files_scanned (int): Number of Allrun files found and scanned.
            results (list): validate_allrun() result for each file.
            dangerous_summary (list): All dangerous commands across all files.
    """
    audit = {
        'is_safe': True,
        'files_scanned': 0,
        'results': [],
        'dangerous_summary': [],
    }

    run_path = Path(run_dir)
    if not run_path.exists():
        return audit

    # Find all Allrun files (Allrun, Allrun.pre, etc.)
    # Exclude log/output artifacts (.log, .err, .out, .bak)
    allrun_files = sorted(
        f for f in run_path.glob('**/Allrun*')
        if f.is_file() and f.suffix not in ('.log', '.err', '.out', '.bak', '.orig')
    )

    for allrun_file in allrun_files:
        audit['files_scanned'] += 1
        validation = validate_allrun(str(allrun_file))
        audit['results'].append(validation)

        if not validation['is_safe']:
            audit['is_safe'] = False
            for dc in validation['dangerous_commands']:
                audit['dangerous_summary'].append({
                    'file': str(allrun_file.relative_to(run_path)),
                    **dc,
                })

    return audit
