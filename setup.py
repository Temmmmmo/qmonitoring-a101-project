"""Build data-free pyRevit package resources without importing the application."""
from pathlib import Path
import runpy

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


class BuildWithRevitBundles(build_py):
    def run(self):
        super().run()
        helpers = runpy.run_path(str(ROOT / "src/rebar/application/revit_installation.py"))
        self.revit_outputs = helpers["build_distributable_bundles"](
            ROOT / "integrations/pyrevit", Path(self.build_lib) / "rebar/web/revit_bundles")

    def get_outputs(self, include_bytecode=1):
        return [*super().get_outputs(include_bytecode), *getattr(self, "revit_outputs", [])]


setup(cmdclass={"build_py": BuildWithRevitBundles})
