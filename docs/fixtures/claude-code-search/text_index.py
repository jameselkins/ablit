from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    id: str
    text: str


class Index:
    def __init__(self, documents):
        self.documents = list(documents)

    def search(self, query):
        words = query.split()
        matches = []
        for document in self.documents:
            score = sum(document.text.split().count(word) for word in words)
            if score:
                matches.append((document.id, score))
        return [document_id for document_id, _ in sorted(matches, key=lambda row: -row[1])]
