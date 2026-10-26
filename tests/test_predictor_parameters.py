#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Behnam Farnaghinejad <behnam.farnaghinejad@polito.it>
# SPDX-License-Identifier: GPL-2.0-only
"""Validate editable predictor settings without changing legacy trace defaults."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "ase_predictor_backend", Path(__file__).resolve().parents[1] / "backend.py")
backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backend)


class PredictorParameterTests(unittest.TestCase):
    def configuration(self, **changes):
        with mock.patch.object(backend, "ENABLE_MULTI_ISSUE_CPU", True):
            return backend.validate_config({**backend.DEFAULT_O3_CONFIG, **changes})

    def test_existing_projects_receive_unchanged_defaults(self):
        old = {key: value for key, value in backend.DEFAULT_O3_CONFIG.items()
               if key != "branchPredictorParameters"}
        config = self.configuration(**old)
        self.assertEqual(config["branchPredictorParameters"],
                         backend.DEFAULT_PREDICTOR_PARAMETERS)
        self.assertEqual(config["branchPredictorParameters"]["local"], {
            "localPredictorSize": 32, "localCtrBits": 2, "BTBEntries": 256,
        })
        self.assertEqual(config["branchPredictorParameters"]["tage"]["maxHist"], 130)

    def test_tournament_history_and_counter_parameters_can_change(self):
        config = self.configuration(branchPredictor="tournament",
            branchPredictorParameters={"tournament": {
                "globalPredictorSize": 256, "globalCtrBits": 3,
                "choicePredictorSize": 256,
            }})
        settings = config["branchPredictorParameters"]["tournament"]
        self.assertEqual((settings["globalPredictorSize"], settings["globalCtrBits"]),
                         (256, 3))
        self.assertEqual(settings["localPredictorSize"], 2048)

    def test_tage_table_parameters_can_change(self):
        config = self.configuration(branchPredictor="tage",
            branchPredictorParameters={"tage": {
                "minHist": 6, "maxHist": 192,
                "tagTableTagWidths": [0, 9, 9, 10, 10, 11, 11, 12],
                "logTagTableSizes": [13, 9, 9, 9, 9, 9, 9, 9],
            }})
        self.assertEqual(config["branchPredictorParameters"]["tage"]["maxHist"], 192)

    def test_bad_table_sizes_and_tage_arrays_are_rejected(self):
        for values in (
            {"local": {"localPredictorSize": 30}},
            {"local": {"localCtrBits": 3}},
            {"tournament": {"globalPredictorSize": 300}},
            {"tage": {"tagTableTagWidths": [0, 9]}},
            {"tage": {"logTagTableSizes": [13, 9, 9, 9, 9, 9, 9, 7]}},
            {"tage": {"tagTableUBits": 3}},
            {"tage": {"maxHist": 5}},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.configuration(branchPredictorParameters=values)

    def test_validation_does_not_share_mutable_table_arrays(self):
        first = self.configuration()
        first["branchPredictorParameters"]["tage"]["tagTableTagWidths"][1] = 15
        second = self.configuration()
        self.assertEqual(second["branchPredictorParameters"]["tage"]["tagTableTagWidths"][1], 9)

    def test_parameters_survive_project_save_and_reload(self):
        with tempfile.TemporaryDirectory(prefix="ase-predictor-config-") as temporary:
            folder = Path(temporary)
            with mock.patch.object(backend, "ENABLE_MULTI_ISSUE_CPU", True):
                backend.save_project_config(folder, {
                    **backend.DEFAULT_O3_CONFIG,
                    "branchPredictor": "tournament",
                    "branchPredictorParameters": {"tournament": {
                        "globalPredictorSize": 256, "globalCtrBits": 3,
                        "choicePredictorSize": 256,
                    }},
                })
                reloaded = backend.project_config(folder)
        self.assertEqual(
            reloaded["branchPredictorParameters"]["tournament"]["globalCtrBits"], 3)


if __name__ == "__main__":
    unittest.main()
