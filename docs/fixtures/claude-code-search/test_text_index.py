import unittest
from text_index import Document, Index


class SearchTests(unittest.TestCase):
    def test_case_is_ignored(self):
        self.assertEqual(Index([Document("a", "Red FOX")]).search("fox"), ["a"])

    def test_punctuation_is_ignored(self):
        self.assertEqual(Index([Document("a", "red, fox!")]).search("fox?"), ["a"])

    def test_scores_count_distinct_query_words(self):
        index = Index([Document("a", "red red red"), Document("b", "red fox")])
        self.assertEqual(index.search("red red fox"), ["b", "a"])

    def test_equal_scores_sort_by_id(self):
        index = Index([Document("z", "fox"), Document("a", "fox")])
        self.assertEqual(index.search("fox"), ["a", "z"])

    def test_empty_query_has_no_matches(self):
        self.assertEqual(Index([Document("a", "fox")]).search(""), [])

    def test_nonmatching_query_has_no_matches(self):
        self.assertEqual(Index([Document("a", "fox")]).search("owl"), [])


if __name__ == "__main__":
    unittest.main()
