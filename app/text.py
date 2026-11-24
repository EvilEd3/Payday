"""Small text helpers."""
import re

# Short words that are ordinary words, not initials, when a bank writes them in capitals
_WORDS = {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "via", "my", "our",
          "new", "old", "pak", "n", "go", "bar", "pub", "fee", "pay", "day", "car", "box", "one", "two", "top",
          "big", "red", "tea", "bay", "st", "rd", "ave", "hot", "art",
          "co", "ltd", "inc", "dd", "ap", "bus", "gym", "spa", "bed", "pet", "pets", "vet", "dr", "mt", "nth", "sth"}


def nice_case(text: str) -> str:
    """Title-case a bank's ALL-CAPS text, keeping short initials as they are: "ANZ HOME LOAN" -> "ANZ Home Loan",
    "AA INSURANCE" -> "AA Insurance", "PAK N SAVE" -> "Pak N Save". Text already in mixed case is left alone."""
    if any(c.islower() for c in text):
        return text

    def word(m: re.Match) -> str:
        w = m.group(0)
        if len(w) <= 3 and w.isalpha() and w.lower() not in _WORDS:
            return w  # initials: ANZ, AA, IRD, BP, NZ
        return w[:1] + w[1:].lower()
    return re.sub(r"[A-Za-z][A-Za-z']*", word, text)
