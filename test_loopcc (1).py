"""Unit tests:  python3 -m unittest -v test_loopcc"""
import unittest
from loopcc import compile_source


def run(src):
    return compile_source(src).diag


class ValidLoops(unittest.TestCase):
    def test_for_while_do(self):
        d = run("int i; for (i = 0; i < 3; i++) { } int j = 0; while (j < 3) { j++; } "
                "int k = 0; do { k++; } while (k < 3);")
        self.assertEqual(d.errors, 0)
        self.assertEqual(d.warnings, 0)

    def test_for_with_declaration_and_multiple_updates(self):
        self.assertEqual(run("for (int i = 0, j = 5; i < j; i++, j--) { }").errors, 0)

    def test_empty_for_header_with_break(self):
        d = run("int x = 0; for (;;) { x++; if (x > 3) break; }")
        self.assertEqual((d.errors, d.warnings), (0, 0))

    def test_nested_loops(self):
        self.assertEqual(run("for (int i=0;i<3;i++) { for (int j=0;j<3;j++) { continue; } }").errors, 0)


class SyntaxErrors(unittest.TestCase):
    def test_missing_semicolon_in_for(self):
        self.assertTrue(any("for-loop condition" in m for m in run("int i; for (i=0; i<3 i++) {}").messages("error")))

    def test_commas_instead_of_semicolons(self):
        self.assertTrue(any("not ','" in m for m in run("for (int i = 0, i < 3, i++) { }").messages("error")))

    def test_missing_paren_in_while(self):
        self.assertGreaterEqual(run("int x=0; while (x < 3 { x++; }").errors, 1)

    def test_do_while_needs_semicolon(self):
        self.assertTrue(any("do-while must end" in m for m in run("int x=0; do { x++; } while (x<3)").messages("error")))

    def test_empty_condition(self):
        self.assertTrue(any("empty condition" in m for m in run("while () { }").messages("error")))

    def test_missing_body(self):
        self.assertGreaterEqual(run("int i; for (i=0;i<3;i++)").errors, 1)

    def test_unclosed_brace(self):
        self.assertTrue(any("unclosed" in m for m in run("while (1) { ").messages("error")))

    def test_bad_assignment_target(self):
        self.assertGreaterEqual(run("int i; for (i+1 = 0; i<3; i++) {}").errors, 1)

    def test_recovers_and_reports_multiple_errors(self):
        self.assertGreaterEqual(run("int i; for (i=0; i<3 i++) {} while (i<3 { }").errors, 2)


class SemanticChecks(unittest.TestCase):
    def test_undeclared(self):
        self.assertGreaterEqual(run("for (i = 0; i < 3; i++) { }").errors, 1)

    def test_redeclaration(self):
        self.assertGreaterEqual(run("int a; int a;").errors, 1)

    def test_for_scope_ends_with_loop(self):
        self.assertGreaterEqual(run("for (int i=0;i<3;i++) {} i = 1;").errors, 1)

    def test_break_outside_loop(self):
        self.assertGreaterEqual(run("break;").errors, 1)

    def test_division_by_zero(self):
        self.assertGreaterEqual(run("int x = 4 / 0;").errors, 1)

    def test_assignment_in_condition(self):
        self.assertTrue(any("did you mean" in m for m in run("int i=0; while (i = 1) { i++; }").messages("warning")))

    def test_update_without_effect(self):
        self.assertTrue(any("no effect" in m for m in run("for (int i=0;i<3;i+1) { }").messages("warning")))

    def test_wrong_direction(self):
        self.assertTrue(any("never terminate" in m for m in run("for (int i=0;i<3;i--) { }").messages("warning")))

    def test_constant_true_condition(self):
        self.assertTrue(any("infinite loop" in m for m in run("while (1) { }").messages("warning")))

    def test_constant_false_condition(self):
        self.assertTrue(any("never executes" in m for m in run("while (0) { }").messages("warning")))

    def test_empty_body(self):
        self.assertTrue(any("empty body" in m for m in run("for (int i=0;i<3;i++);").messages("warning")))

    def test_uninitialized(self):
        self.assertTrue(any("before being initialized" in m for m in run("int u; while (u < 3) { u++; }").messages("warning")))


class IntermediateCode(unittest.TestCase):
    def test_tac_for(self):
        tac = compile_source("int s = 0; for (int i = 0; i < 3; i++) { s += i; }").tac
        text = "\n".join(tac)
        self.assertIn("ifFalse", text)
        self.assertIn("goto", text)
        self.assertIn("i = i + 1", text)

    def test_tac_do_while_tests_at_bottom(self):
        tac = compile_source("int k = 0; do { k++; } while (k < 3);").tac
        self.assertTrue(any(line.strip().startswith("if t") for line in tac))


if __name__ == "__main__":
    unittest.main()
