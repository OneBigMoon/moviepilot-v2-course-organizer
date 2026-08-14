import importlib.util
import sys
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).parents[1] / "plugins.v2" / "courseorganizer" / "__init__.py"
)


def load_courseorganizer():
    sys.modules.pop("courseorganizer", None)
    for key in list(sys.modules):
        if key == "courseorganizer" or key.startswith("courseorganizer."):
            sys.modules.pop(key, None)
    spec = importlib.util.spec_from_file_location(
        "courseorganizer",
        PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
