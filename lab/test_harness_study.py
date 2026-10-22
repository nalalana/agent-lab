import unittest
from build_eval_corpus import synthetic
from prepare_harness_study import select
from summarize_harness_study import paired


class StudySelectionTests(unittest.TestCase):
    def test_paired_comparison_preserves_improvements_and_regressions(self):
        before = [{"id": "a", "correct": False}, {"id": "b", "correct": True}]
        after = [{"id": "b", "correct": False}, {"id": "a", "correct": True}]
        self.assertEqual(paired(before, after), {"improved": ["a"], "regressed": ["b"]})
        with self.assertRaises(ValueError):
            paired(before, after[:1])

    def test_complete_balanced_entity_groups_and_disjoint_splits(self):
        rows = list(synthetic())
        dev = select([r for r in rows if r["split"] == "dev"], "synthetic", 2)
        validation = select([r for r in rows if r["split"] == "validation"], "synthetic", 3)
        self.assertEqual((len(dev), len(validation)), (64, 96))
        self.assertEqual(sum(r["supported"] for r in dev), 32)
        self.assertEqual(len({r["family"] for r in dev}), 8)
        self.assertFalse({r["group"] for r in dev} & {r["group"] for r in validation})


if __name__ == "__main__":
    unittest.main()
