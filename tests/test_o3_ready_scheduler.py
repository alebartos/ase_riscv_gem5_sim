#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Behnam Farnaghinejad <behnam.farnaghinejad@polito.it>
# SPDX-License-Identifier: GPL-2.0-only
"""Ready work, not program-order future reservations, acquires teaching EXE."""
from collections import Counter
import importlib.util
from pathlib import Path
import random
import unittest

SPEC = importlib.util.spec_from_file_location(
    "ase_ready_backend", Path(__file__).resolve().parents[1] / "backend.py")
backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backend)


class ReadySchedulerTests(unittest.TestCase):
    @staticmethod
    def rows(instructions):
        return [{"instruction": text, "address": f"{0x10000 + 4 * i:x}",
                 "sequence": i + 1, "cycles": {str(i + 1): "I", str(i + 10): "W"}}
                for i, text in enumerate(instructions)]

    def schedule(self, rows, **settings):
        data = {"format": "o3", "dynamicInstructions": rows, "jumps": []}
        config = {**backend.DEFAULT_O3_CONFIG, "floatDiv": 8, **settings}
        backend.schedule_lecture_o3_pipeline(data, config)
        backend.configure_o3_stage_display(data, config)
        return data

    @staticmethod
    def stage(row, marker):
        return sorted(int(c) for c, stage in row["cycles"].items() if marker in stage)

    def test_younger_ready_divide_uses_gap_before_older_dependent_divide(self):
        rows = self.rows(["fdiv.s f1, f20, f21", "fdiv.s f2, f1, f22",
                          "fdiv.s f3, f23, f24"])
        self.schedule(rows)
        self.assertEqual([self.stage(r, "E")[0] for r in rows], [2, 18, 10])
        self.assertEqual(self.stage(rows[0], "C"), [10])

    def test_oldest_ready_wins_not_the_instruction_ready_for_the_longest_time(self):
        rows = self.rows(["fdiv.s f1, f20, f21", "fdiv.s f2, f1, f22",
                          "fdiv.s f3, f23, f24", "fdiv.s f4, f25, f26"])
        self.schedule(rows)
        self.assertEqual([self.stage(r, "E")[0] for r in rows], [2, 18, 10, 26])

    def test_next_iteration_divide_overtakes_waiting_first_iteration_consumer(self):
        body = ["flw f1, 0(x1)", "flw f2, 0(x2)", "flw f3, 0(x3)",
                "fdiv.s f4, f1, f2", "fdiv.s f4, f4, f3", "fsw f4, 0(x4)",
                "addi x1, x1, 4", "addi x2, x2, 4", "addi x3, x3, 4",
                "addi x4, x4, 4", "addi x5, x5, -1", "bne x5, x0, -44"]
        rows = self.rows(body * 3)
        self.schedule(rows)
        self.assertEqual(self.stage(rows[3], "E"), list(range(6, 14)))
        self.assertEqual(self.stage(rows[15], "E"), list(range(14, 22)))
        self.assertEqual(self.stage(rows[4], "E"), list(range(22, 30)))
        self.assertEqual(self.stage(rows[16], "E"), list(range(30, 38)))
        commits = [self.stage(r, "W")[0] for r in rows]
        self.assertEqual(commits, sorted(commits))

    def test_renaming_removes_false_waw_and_war_but_preserves_true_raw(self):
        rows = self.rows(["fdiv.s f1, f2, f3", "fdiv.s f4, f1, f5",
                          "fdiv.s f1, f6, f7"])
        self.schedule(rows)
        self.assertEqual([self.stage(r, "E")[0] for r in rows], [2, 18, 10])
        self.assertGreater(self.stage(rows[1], "E")[0], self.stage(rows[0], "C")[0])
        self.assertEqual(self.stage(rows[1], "E")[0], self.stage(rows[2], "C")[0])

    def test_x0_writes_create_no_producer_but_still_read_their_sources(self):
        rows = self.rows(["mul x5, x20, x21", "add x0, x5, x6",
                          "div x7, x20, x21"])
        self.schedule(rows, intMul=4, intDiv=8)
        self.assertEqual(self.stage(rows[1], "E"), [7])
        self.assertEqual(self.stage(rows[1], "C"), [])
        self.assertEqual(self.stage(rows[2], "E")[0], 3)

    def test_jalr_to_x0_reads_but_does_not_rename_its_base_register(self):
        rows = self.rows(["addi x1, x0, 16", "jalr x0, 0(x1)"])
        self.schedule(rows, intAlu=4)
        self.assertEqual(self.stage(rows[1], "E"), [7])
        self.assertEqual(self.stage(rows[1], "C"), [])

    def test_log_sequence_numbers_define_program_order(self):
        rows = self.rows(["addi x5, x0, 1", "add x6, x5, x5"])
        data = self.schedule(list(reversed(rows)))
        self.assertEqual([r["sequence"] for r in data["dynamicInstructions"]], [1, 2])
        self.assertGreater(self.stage(rows[1], "E")[0], self.stage(rows[0], "C")[0])

    def test_gem5_values_are_relocated_to_commit_not_recomputed_from_execution_order(self):
        rows = self.rows(["fdiv.s f4, f1, f2", "fdiv.s f4, f4, f3",
                          "fsw f4, 0(x8)", "fdiv.s f4, f1, f2"])
        data = {"format": "o3", "dynamicInstructions": rows, "jumps": [],
                "registerDeltas": {"1": {"f4": "0x3f800000"},
                                   "2": {"f4": "0x40000000"},
                                   "4": {"f4": "0x40400000"}},
                "memoryDeltas": {"3": {"0x20000": {
                    "pc": "0x10008", "access": "write", "value": "0x40000000"}}}}
        config = {**backend.DEFAULT_O3_CONFIG, "floatDiv": 8, "commitWidth": 1}
        backend.schedule_lecture_o3_pipeline(data, config)
        backend.configure_o3_stage_display(data, config)
        self.assertLess(self.stage(rows[3], "E")[0], self.stage(rows[1], "E")[0])
        writes = [changes["f4"] for cycle, changes in sorted(
            data["registerDeltas"].items(), key=lambda item: int(item[0]))]
        self.assertEqual(writes, ["0x3f800000", "0x40000000", "0x40400000"])
        self.assertEqual(data["memoryDeltas"][str(self.stage(rows[2], "W")[0])]
                         ["0x20000"]["value"], "0x40000000")

    def test_unit_bandwidth_cdb_commit_and_raw_limits(self):
        instructions = ["fdiv.s f1, f20, f21", "fdiv.s f2, f1, f22",
                        "fdiv.s f3, f23, f24", "fdiv.s f4, f25, f26",
                        "fadd.s f5, f2, f3", "fadd.s f6, f4, f5"]
        for units in (1, 2):
            for pipelined in (False, True):
                with self.subTest(units=units, pipelined=pipelined):
                    rows = self.rows(instructions)
                    self.schedule(rows, floatDivUnits=units, floatDivPipelined=pipelined)
                    uses = Counter(c for r in rows[:4] for c in
                                   (self.stage(r, "E")[:1] if pipelined else self.stage(r, "E")))
                    self.assertLessEqual(max(uses.values()), units)
                    for marker in ("C", "W"):
                        uses = Counter(c for r in rows for c in self.stage(r, marker))
                        self.assertLessEqual(max(uses.values()), 2)
                    for consumer, producer in ((1, 0), (4, 1), (4, 2), (5, 3), (5, 4)):
                        self.assertGreater(self.stage(rows[consumer], "E")[0],
                                           self.stage(rows[producer], "C")[0])

    def test_no_idle_fu_skips_an_issued_ready_instruction(self):
        # Check the final schedule against an independent readiness predicate,
        # including repeated register names and different FU initiation rates.
        for seed in range(16):
            rng = random.Random(seed)
            instructions = [f"{rng.choice(('fadd.s', 'fmul.s', 'fdiv.s'))} "
                            f"f{rng.randint(1, 6)}, f{rng.randint(1, 8)}, f{rng.randint(1, 8)}"
                            for _ in range(24)]
            rows = self.rows(instructions)
            units = 1 + seed % 2
            pipelined_divide = bool(seed % 3)
            self.schedule(rows, floatDivUnits=units, floatDivPipelined=pipelined_divide)
            dependencies, versions = [], {}
            for index, instruction in enumerate(instructions):
                fields = instruction.replace(',', '').split()
                dependencies.append([versions[r] for r in fields[2:] if r in versions])
                versions[fields[1]] = index
            starts = [self.stage(row, 'E')[0] for row in rows]
            ends = [self.stage(row, 'E')[-1] for row in rows]
            issues = [self.stage(row, 'I')[0] for row in rows]
            broadcasts = [self.stage(row, 'C')[0] for row in rows]
            for index in range(len(rows)):
                self.assertGreater(starts[index], issues[index])
                self.assertGreater(broadcasts[index], ends[index])
                for producer in dependencies[index]:
                    self.assertGreater(starts[index], broadcasts[producer])
            for opcode in ('fadd.s', 'fmul.s', 'fdiv.s'):
                members = [i for i, text in enumerate(instructions) if text.startswith(opcode)]
                count = units if opcode == 'fdiv.s' else 1
                pipelined = pipelined_divide if opcode == 'fdiv.s' else True
                for clock in range(2, max(ends) + 1):
                    busy = 0 if pipelined else sum(starts[i] < clock <= ends[i] for i in members)
                    ready = [i for i in members if starts[i] >= clock and issues[i] < clock
                             and all(broadcasts[p] < clock for p in dependencies[i])]
                    actual = [i for i in members if starts[i] == clock]
                    self.assertEqual(actual, ready[:count - busy], (seed, opcode, clock))


if __name__ == "__main__":
    unittest.main()
