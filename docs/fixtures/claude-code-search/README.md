# Text search

The Index class searches Document objects. Search should ignore case and punctuation. Each distinct query word contributes one point if it appears anywhere in a document. Repeated words in the query or document must not increase the score. Results contain document IDs, sorted by descending score and then ascending ID. An empty query returns no results.

Run the tests with `python3 -m unittest -v`.
