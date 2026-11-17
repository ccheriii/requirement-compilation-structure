import unittest

from infrastructure.pipeline.backend_implementation import IMPLEMENT_INSTRUCTIONS, TEST_INSTRUCTIONS
from infrastructure.stages.frontend.triggers.implementation import TEST_INSTRUCTIONS as E2E_TEST_INSTRUCTIONS


class PromptCoverage(unittest.TestCase):
    def test_backend_tests_cover_distinct_contract_branches_without_a_hard_count_cap(self):
        self.assertIn("Treat max_tests as a normal-suite size target, not a hard coverage limit", TEST_INSTRUCTIONS)
        self.assertIn("each distinct response.errors/precondition branch", TEST_INSTRUCTIONS)
        self.assertIn("every success and error branch declared by task.api", IMPLEMENT_INSTRUCTIONS)
        self.assertIn("silently converted\nto a success-shaped response", IMPLEMENT_INSTRUCTIONS)

    def test_e2e_tests_cover_distinct_requirement_outcomes_without_a_hard_count_cap(self):
        self.assertIn("each distinct user-visible scenario outcome", E2E_TEST_INSTRUCTIONS)
        self.assertIn("not a hard cap", E2E_TEST_INSTRUCTIONS)
        self.assertIn("omit a distinct Given/When/Then behavior merely to meet", E2E_TEST_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
