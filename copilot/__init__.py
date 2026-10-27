"""AI PCB Copilot: safety, live view, review and SPICE tools that sit beside the KiCad MCP engine."""

# No console windows for kicad-cli / ngspice / git children on Windows (the MCP server has no console).
import importlib.util as _ilu, os as _os
_q = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'tools', '_quiet.py')
if _os.path.exists(_q):
    _spec = _ilu.spec_from_file_location('_quiet', _q); _spec.loader.exec_module(_ilu.module_from_spec(_spec))
