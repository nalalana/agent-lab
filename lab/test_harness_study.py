import unittest
from build_eval_corpus import synthetic
from prepare_harness_study import select


class StudySelectionTests(unittest.TestCase):
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
