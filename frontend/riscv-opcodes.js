/*
 * SPDX-License-Identifier: GPL-2.0-only
 *
 * The editor uses this small, dependency-free catalog for completion and
 * reference help.  The active extension set still comes from the backend so
 * this file does not define the project's ISA.
 */
const RISCV_OPCODE_CATALOG = (() => {
  const entries = [];

  const operand = (kind, role, placeholder, display = role) => ({
    kind, role, placeholder, display
  });
  const intReg = (role, placeholder = "x1") =>
    operand("integer-register", role, placeholder);
  const floatReg = (role, placeholder = "f1") =>
    operand("floating-register", role, placeholder);
  const immediate = (role = "immediate", placeholder = "0") =>
    operand("immediate", role, placeholder, "imm");
  const target = (role = "target", placeholder = "target") =>
    operand("label", role, placeholder, "label");
  const csr = (role = "CSR", placeholder = "0x300") =>
    operand("csr", role, placeholder, "csr");
  const fenceSet = (role, placeholder = "iorw") =>
    operand("fence-set", role, placeholder, "iorw");

  function add(mnemonic, extension, syntax, template, operands, description) {
    entries.push({mnemonic, extension, syntax, template, operands, description});
  }

  const r = (mnemonic, extension = "I", description = "Integer register operation") =>
    add(mnemonic, extension, "rd, rs1, rs2", `${mnemonic} {0}, {1}, {2}`,
      [intReg("destination", "x1"), intReg("source 1", "x2"), intReg("source 2", "x3")], description);
  const i = (mnemonic, extension = "I", description = "Integer immediate operation") =>
    add(mnemonic, extension, "rd, rs1, imm", `${mnemonic} {0}, {1}, {2}`,
      [intReg("destination", "x1"), intReg("source", "x2"), immediate()], description);
  const shift = (mnemonic, description = "Integer shift operation") =>
    add(mnemonic, "I", "rd, rs1, shamt", `${mnemonic} {0}, {1}, {2}`,
      [intReg("destination", "x1"), intReg("source", "x2"), immediate("shift amount")], description);

  add("lui", "I", "rd, imm", "lui {0}, {1}",
    [intReg("destination", "x1"), immediate()], "Load upper immediate");
  add("auipc", "I", "rd, imm", "auipc {0}, {1}",
    [intReg("destination", "x1"), immediate()], "Add upper immediate to PC");
  add("jal", "I", "rd, label", "jal {0}, {1}",
    [intReg("destination", "x1"), target()], "Jump and link");
  add("jalr", "I", "rd, rs1, imm", "jalr {0}, {1}, {2}",
    [intReg("destination", "x1"), intReg("base", "x2"), immediate()], "Jump and link register");

  ["beq", "bne", "blt", "bge", "bltu", "bgeu"].forEach(mnemonic => {
    add(mnemonic, "I", "rs1, rs2, label", `${mnemonic} {0}, {1}, {2}`,
      [intReg("source 1", "x1"), intReg("source 2", "x2"), target()], "Conditional branch");
  });

  ["lb", "lh", "lw", "lbu", "lhu"].forEach(mnemonic => {
    add(mnemonic, "I", "rd, imm(rs1)", `${mnemonic} {0}, {1}({2})`,
      [intReg("destination", "x1"), immediate(), intReg("base", "x2")], "Load from memory");
  });
  ["sb", "sh", "sw"].forEach(mnemonic => {
    add(mnemonic, "I", "rs2, imm(rs1)", `${mnemonic} {0}, {1}({2})`,
      [intReg("source", "x1"), immediate(), intReg("base", "x2")], "Store to memory");
  });

  ["addi", "slti", "sltiu", "xori", "ori", "andi"].forEach(mnemonic => i(mnemonic));
  ["slli", "srli", "srai"].forEach(mnemonic => shift(mnemonic));
  ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"]
    .forEach(mnemonic => r(mnemonic));
  add("fence", "I", "pred, succ", "fence {0}, {1}",
    [fenceSet("predecessor"), fenceSet("successor")], "Memory-ordering fence");
  add("fence.i", "Zifencei", "", "fence.i", [], "Instruction-stream fence");
  add("ecall", "I", "", "ecall", [], "Environment call");
  add("ebreak", "I", "", "ebreak", [], "Environment breakpoint");

  ["csrrw", "csrrs", "csrrc"].forEach(mnemonic => {
    add(mnemonic, "Zicsr", "rd, csr, rs1", `${mnemonic} {0}, {1}, {2}`,
      [intReg("destination", "x1"), csr(), intReg("source", "x2")], "CSR register operation");
  });
  ["csrrwi", "csrrsi", "csrrci"].forEach(mnemonic => {
    add(mnemonic, "Zicsr", "rd, csr, uimm", `${mnemonic} {0}, {1}, {2}`,
      [intReg("destination", "x1"), csr(), immediate("unsigned immediate", "1")], "CSR immediate operation");
  });

  ["mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu"]
    .forEach(mnemonic => r(mnemonic, "M", "Integer multiply or divide operation"));

  add("lr.w", "A", "rd, (rs1)", "lr.w {0}, ({1})",
    [intReg("destination", "x1"), intReg("base", "x2")], "Load-reserved word");
  add("sc.w", "A", "rd, rs2, (rs1)", "sc.w {0}, {1}, ({2})",
    [intReg("destination", "x1"), intReg("source", "x3"), intReg("base", "x2")], "Store-conditional word");
  ["amoswap.w", "amoadd.w", "amoxor.w", "amoand.w", "amoor.w",
    "amomin.w", "amomax.w", "amominu.w", "amomaxu.w"].forEach(mnemonic => {
    add(mnemonic, "A", "rd, rs2, (rs1)", `${mnemonic} {0}, {1}, ({2})`,
      [intReg("destination", "x1"), intReg("source", "x3"), intReg("base", "x2")], "Atomic memory operation");
  });

  add("flw", "F", "fd, imm(rs1)", "flw {0}, {1}({2})",
    [floatReg("destination", "f1"), immediate(), intReg("base", "x2")], "Load single-precision value");
  add("fsw", "F", "fs2, imm(rs1)", "fsw {0}, {1}({2})",
    [floatReg("source", "f1"), immediate(), intReg("base", "x2")], "Store single-precision value");
  ["fadd.s", "fsub.s", "fmul.s", "fdiv.s", "fmin.s", "fmax.s",
    "fsgnj.s", "fsgnjn.s", "fsgnjx.s"].forEach(mnemonic => {
    add(mnemonic, "F", "fd, fs1, fs2", `${mnemonic} {0}, {1}, {2}`,
      [floatReg("destination", "f1"), floatReg("source 1", "f2"), floatReg("source 2", "f3")], "Single-precision operation");
  });
  add("fsqrt.s", "F", "fd, fs1", "fsqrt.s {0}, {1}",
    [floatReg("destination", "f1"), floatReg("source", "f2")], "Single-precision square root");
  ["feq.s", "flt.s", "fle.s"].forEach(mnemonic => {
    add(mnemonic, "F", "rd, fs1, fs2", `${mnemonic} {0}, {1}, {2}`,
      [intReg("destination", "x1"), floatReg("source 1", "f2"), floatReg("source 2", "f3")], "Single-precision comparison");
  });
  add("fclass.s", "F", "rd, fs1", "fclass.s {0}, {1}",
    [intReg("destination", "x1"), floatReg("source", "f2")], "Classify single-precision value");
  ["fcvt.w.s", "fcvt.wu.s"].forEach(mnemonic => {
    add(mnemonic, "F", "rd, fs1", `${mnemonic} {0}, {1}`,
      [intReg("destination", "x1"), floatReg("source", "f2")], "Convert floating point to integer");
  });
  ["fcvt.s.w", "fcvt.s.wu"].forEach(mnemonic => {
    add(mnemonic, "F", "fd, rs1", `${mnemonic} {0}, {1}`,
      [floatReg("destination", "f1"), intReg("source", "x2")], "Convert integer to floating point");
  });
  add("fmv.x.w", "F", "rd, fs1", "fmv.x.w {0}, {1}",
    [intReg("destination", "x1"), floatReg("source", "f2")], "Move float bits to integer register");
  add("fmv.w.x", "F", "fd, rs1", "fmv.w.x {0}, {1}",
    [floatReg("destination", "f1"), intReg("source", "x2")], "Move integer bits to float register");
  ["fmadd.s", "fmsub.s", "fnmsub.s", "fnmadd.s"].forEach(mnemonic => {
    add(mnemonic, "F", "fd, fs1, fs2, fs3", `${mnemonic} {0}, {1}, {2}, {3}`,
      [floatReg("destination", "f1"), floatReg("source 1", "f2"), floatReg("source 2", "f3"), floatReg("source 3", "f4")], "Single-precision fused operation");
  });

  add("fld", "D", "fd, imm(rs1)", "fld {0}, {1}({2})",
    [floatReg("destination", "f1"), immediate(), intReg("base", "x2")], "Load double-precision value");
  add("fsd", "D", "fs2, imm(rs1)", "fsd {0}, {1}({2})",
    [floatReg("source", "f1"), immediate(), intReg("base", "x2")], "Store double-precision value");
  ["fadd.d", "fsub.d", "fmul.d", "fdiv.d", "fmin.d", "fmax.d",
    "fsgnj.d", "fsgnjn.d", "fsgnjx.d"].forEach(mnemonic => {
    add(mnemonic, "D", "fd, fs1, fs2", `${mnemonic} {0}, {1}, {2}`,
      [floatReg("destination", "f1"), floatReg("source 1", "f2"), floatReg("source 2", "f3")], "Double-precision operation");
  });
  add("fsqrt.d", "D", "fd, fs1", "fsqrt.d {0}, {1}",
    [floatReg("destination", "f1"), floatReg("source", "f2")], "Double-precision square root");
  ["feq.d", "flt.d", "fle.d"].forEach(mnemonic => {
    add(mnemonic, "D", "rd, fs1, fs2", `${mnemonic} {0}, {1}, {2}`,
      [intReg("destination", "x1"), floatReg("source 1", "f2"), floatReg("source 2", "f3")], "Double-precision comparison");
  });
  add("fclass.d", "D", "rd, fs1", "fclass.d {0}, {1}",
    [intReg("destination", "x1"), floatReg("source", "f2")], "Classify double-precision value");
  ["fcvt.w.d", "fcvt.wu.d"].forEach(mnemonic => {
    add(mnemonic, "D", "rd, fs1", `${mnemonic} {0}, {1}`,
      [intReg("destination", "x1"), floatReg("source", "f2")], "Convert double precision to integer");
  });
  ["fcvt.d.w", "fcvt.d.wu"].forEach(mnemonic => {
    add(mnemonic, "D", "fd, rs1", `${mnemonic} {0}, {1}`,
      [floatReg("destination", "f1"), intReg("source", "x2")], "Convert integer to double precision");
  });
  ["fcvt.s.d", "fcvt.d.s"].forEach(mnemonic => {
    add(mnemonic, "D", mnemonic === "fcvt.s.d" ? "fd, fs1" : "fd, fs1", `${mnemonic} {0}, {1}`,
      [floatReg("destination", "f1"), floatReg("source", "f2")], "Convert floating-point precision");
  });
  ["fmadd.d", "fmsub.d", "fnmsub.d", "fnmadd.d"].forEach(mnemonic => {
    add(mnemonic, "D", "fd, fs1, fs2, fs3", `${mnemonic} {0}, {1}, {2}, {3}`,
      [floatReg("destination", "f1"), floatReg("source 1", "f2"), floatReg("source 2", "f3"), floatReg("source 3", "f4")], "Double-precision fused operation");
  });

  add("c.nop", "C", "", "c.nop", [], "Compressed no-op");
  add("c.addi", "C", "rd, imm", "c.addi {0}, {1}",
    [intReg("destination", "x1"), immediate()], "Compressed add immediate");
  add("c.li", "C", "rd, imm", "c.li {0}, {1}",
    [intReg("destination", "x1"), immediate()], "Compressed load immediate");
  add("c.lui", "C", "rd, imm", "c.lui {0}, {1}",
    [intReg("destination", "x1"), immediate()], "Compressed load upper immediate");
  add("c.slli", "C", "rd, shamt", "c.slli {0}, {1}",
    [intReg("destination", "x1"), immediate("shift amount")], "Compressed shift left");
  ["c.srli", "c.srai", "c.andi"].forEach(mnemonic => {
    add(mnemonic, "C", "rd, imm", `${mnemonic} {0}, {1}`,
      [intReg("destination", "x8"), immediate()], "Compressed immediate operation");
  });
  ["c.sub", "c.xor", "c.or", "c.and"].forEach(mnemonic => {
    add(mnemonic, "C", "rd', rs2'", `${mnemonic} {0}, {1}`,
      [intReg("destination", "x8"), intReg("source", "x9")], "Compressed register operation");
  });
  add("c.mv", "C", "rd, rs2", "c.mv {0}, {1}",
    [intReg("destination", "x1"), intReg("source", "x2")], "Compressed register move");
  add("c.add", "C", "rd, rs2", "c.add {0}, {1}",
    [intReg("destination", "x1"), intReg("source", "x2")], "Compressed register add");
  add("c.j", "C", "label", "c.j {0}", [target()], "Compressed jump");
  add("c.jal", "C", "label", "c.jal {0}", [target()], "Compressed jump and link");
  add("c.jr", "C", "rs1", "c.jr {0}", [intReg("source", "x1")], "Compressed indirect jump");
  add("c.jalr", "C", "rs1", "c.jalr {0}", [intReg("source", "x1")], "Compressed indirect jump and link");
  ["c.beqz", "c.bnez"].forEach(mnemonic => {
    add(mnemonic, "C", "rs1', label", `${mnemonic} {0}, {1}`,
      [intReg("source", "x8"), target()], "Compressed conditional branch");
  });
  add("c.lw", "C", "rd', imm(rs1')", "c.lw {0}, {1}({2})",
    [intReg("destination", "x9"), immediate(), intReg("base", "x8")], "Compressed load word");
  add("c.sw", "C", "rs2', imm(rs1')", "c.sw {0}, {1}({2})",
    [intReg("source", "x9"), immediate(), intReg("base", "x8")], "Compressed store word");
  add("c.lwsp", "C", "rd, imm(sp)", "c.lwsp {0}, {1}(sp)",
    [intReg("destination", "x1"), immediate()], "Compressed stack load");
  add("c.swsp", "C", "rs2, imm(sp)", "c.swsp {0}, {1}(sp)",
    [intReg("source", "x1"), immediate()], "Compressed stack store");
  add("c.ebreak", "C", "", "c.ebreak", [], "Compressed breakpoint");

  const pseudo = (mnemonic, syntax, template, operands, description) =>
    add(mnemonic, "Pseudo", syntax, template, operands, description);
  pseudo("nop", "", "nop", [], "No-op pseudo-instruction");
  pseudo("li", "rd, imm", "li {0}, {1}",
    [intReg("destination", "x1"), immediate()], "Load immediate pseudo-instruction");
  pseudo("la", "rd, label", "la {0}, {1}",
    [intReg("destination", "x1"), target()], "Load address pseudo-instruction");
  pseudo("mv", "rd, rs1", "mv {0}, {1}",
    [intReg("destination", "x1"), intReg("source", "x2")], "Register move pseudo-instruction");
  pseudo("fmv.s", "fd, fs1", "fmv.s {0}, {1}",
    [floatReg("destination", "f1"), floatReg("source", "f2")], "Single-precision floating-point move pseudo-instruction");
  pseudo("fmv.d", "fd, fs1", "fmv.d {0}, {1}",
    [floatReg("destination", "f1"), floatReg("source", "f2")], "Double-precision floating-point move pseudo-instruction");
  pseudo("not", "rd, rs1", "not {0}, {1}",
    [intReg("destination", "x1"), intReg("source", "x2")], "Bitwise-not pseudo-instruction");
  pseudo("neg", "rd, rs1", "neg {0}, {1}",
    [intReg("destination", "x1"), intReg("source", "x2")], "Negation pseudo-instruction");
  ["seqz", "snez", "sltz", "sgtz"].forEach(mnemonic => pseudo(
    mnemonic, "rd, rs1", `${mnemonic} {0}, {1}`,
    [intReg("destination", "x1"), intReg("source", "x2")], "Set-condition pseudo-instruction"));
  ["j", "call", "tail"].forEach(mnemonic => pseudo(
    mnemonic, "label", `${mnemonic} {0}`, [target()], "Control-flow pseudo-instruction"));
  pseudo("jr", "rs1", "jr {0}", [intReg("source", "x1")], "Indirect-jump pseudo-instruction");
  pseudo("ret", "", "ret", [], "Return pseudo-instruction");
  ["beqz", "bnez", "bltz", "bgez", "blez", "bgtz"].forEach(mnemonic => pseudo(
    mnemonic, "rs1, label", `${mnemonic} {0}, {1}`,
    [intReg("source", "x1"), target()], "Conditional-branch pseudo-instruction"));
  ["bgt", "ble", "bgtu", "bleu"].forEach(mnemonic => pseudo(
    mnemonic, "rs1, rs2, label", `${mnemonic} {0}, {1}, {2}`,
    [intReg("source 1", "x1"), intReg("source 2", "x2"), target()], "Conditional-branch pseudo-instruction"));
  ["csrr", "csrw", "csrs", "csrc"].forEach(mnemonic => pseudo(
    mnemonic, mnemonic === "csrr" ? "rd, csr" : "csr, rs1",
    mnemonic === "csrr" ? `${mnemonic} {0}, {1}` : `${mnemonic} {0}, {1}`,
    mnemonic === "csrr" ? [intReg("destination", "x1"), csr()]
      : [csr(), intReg("source", "x1")], "CSR pseudo-instruction"));
  ["rdcycle", "rdtime", "rdinstret"].forEach(mnemonic => pseudo(
    mnemonic, "rd", `${mnemonic} {0}`, [intReg("destination", "x1")], "Counter-read pseudo-instruction"));

  return entries.sort((left, right) => left.mnemonic.localeCompare(right.mnemonic));
})();
