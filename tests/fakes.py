"""Canned LLM payloads shared by tests."""
import json

TOPICS = {
    "course_title": "MATH 33A: Linear Algebra",
    "topics": [
        {"title": "Vectors as arrows",
         "objectives": ["Draw a vector as an arrow from the origin", "Add vectors tip to tail",
                        "Scale a vector by a number"],
         "prerequisites": []},
        {"title": "Linear transformations",
         "objectives": ["Describe a transformation by where basis vectors land",
                        "Recognize rotations and shears", "Compute a matrix-vector product"],
         "prerequisites": ["Vectors as arrows"]},
        {"title": "The determinant",
         "objectives": ["Interpret the determinant as area scaling", "Compute a 2x2 determinant",
                        "Explain what a zero determinant means"],
         "prerequisites": ["Linear transformations"]},
    ],
}

_NARR = ("A vector is an arrow that starts at the origin and points somewhere in the plane. "
         "Its two coordinates tell you how far to walk along x and then along y to reach the tip.")


def script(n_segments: int = 4, words_per_segment: int = 45) -> dict:
    base = (_NARR + " ") * 4
    words = base.split()
    segs = []
    for i in range(n_segments):
        segs.append({
            "narration": " ".join(words[:words_per_segment]),
            "visual": f"Segment {i}: draw axes and an arrow from the origin to (2, 1).",
            "key_equation": "\\vec{v} = (2, 1)" if i == 1 else None,
        })
    return {"title": "Vectors as arrows", "segments": segs}


def dumps(obj) -> str:
    return json.dumps(obj)
