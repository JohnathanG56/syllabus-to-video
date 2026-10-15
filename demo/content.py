"""Hand-written, known-good LLM outputs for the offline demo.

Everything here is what a model *would* return for the demo syllabus; the
pipeline treats it exactly like real model output (schema validation, timing
injection, AST validation, sandboxed render, ffprobe checks, assembly).
"""

DEMO_SYLLABUS = """MATH 1D: Two Ideas, Animated (demo syllabus)

Week 1: Vectors as arrows. Coordinates, vector addition (tip to tail) and scalar multiplication.
Week 2: The Pythagorean theorem. Squares on the sides of a right triangle and the distance formula.

Grading: none, this is a demo. Office hours: whenever the render finishes.
"""

TOPICS = {
    "course_title": "MATH 1D: Two Ideas, Animated",
    "topics": [
        {
            "title": "Vectors as arrows",
            "objectives": [
                "Draw a vector as an arrow from the origin",
                "Read a vector's coordinates off the grid",
                "Add two vectors tip to tail",
                "Scale a vector by a number, including a negative one",
            ],
            "prerequisites": [],
        },
        {
            "title": "The Pythagorean theorem",
            "objectives": [
                "Identify the legs and hypotenuse of a right triangle",
                "Interpret a squared and b squared as areas of squares",
                "Use a squared plus b squared equals c squared to find a missing side",
            ],
            "prerequisites": ["Areas of squares"],
        },
    ],
}

SCRIPTS = {
    "Vectors as arrows": {
        "title": "Vectors as arrows",
        "segments": [
            {
                "narration": "Let's start with the simplest object in linear algebra, the vector. Picture an "
                             "arrow that begins at the origin of a grid and points somewhere. Only its length "
                             "and its direction matter. Think of it as an instruction for how to move. We "
                             "will call this arrow v.",
                "visual": "A coordinate grid fades in. A yellow arrow grows from the origin to the point (3, 2) "
                          "and is labeled v.",
                "key_equation": None,
            },
            {
                "narration": "To describe v with numbers, read off where its tip lands. Walk three units along "
                             "the x axis, then two units up. Those two numbers, three and two, stacked in a "
                             "column, are the coordinates of v. Every arrow on this grid matches exactly one "
                             "such pair, so the list and the arrow are the same thing.",
                "visual": "Dashed lines drop from the tip to each axis, a green brace marks 3 along x and a red "
                          "one marks 2 along y; a column vector [3, 2] appears in the corner.",
                "key_equation": r"\vec{v} = \begin{bmatrix} 3 \\ 2 \end{bmatrix}",
            },
            {
                "narration": "Adding vectors has a lovely picture. Take a second vector, w, and slide it so its "
                             "tail sits on the tip of v. The arrow from the origin to where you end up is v plus "
                             "w. With numbers, you simply add the matching coordinates: three plus one is "
                             "four, and two plus negative three is negative one.",
                "visual": "A blue arrow w = (1, -3) appears at the origin, then slides tip to tail onto v. A "
                          "green arrow from the origin to (4, -1) is drawn and labeled v + w.",
                "key_equation": r"\vec{v} + \vec{w} = \begin{bmatrix} 4 \\ -1 \end{bmatrix}",
            },
            {
                "narration": "Multiplying by a number, called a scalar, stretches the arrow. Two times v points "
                             "the same way but is twice as long, and negative one times v flips it around. "
                             "Adding arrows and scaling arrows: that is the whole game of linear algebra.",
                "visual": "v stretches to 2v, then flips to -v. A closing caption reads 'add and scale'.",
                "key_equation": r"2\vec{v}, \quad -\vec{v}",
            },
        ],
    },
    "The Pythagorean theorem": {
        "title": "The Pythagorean theorem",
        "segments": [
            {
                "narration": "Here is a right triangle. The two shorter sides that meet at the square corner are "
                             "called the legs, a and b. The long side across from the right angle is the "
                             "hypotenuse, c. The little square in the corner marks the ninety degree angle. "
                             "Let's make the legs three and four units long.",
                "visual": "A right triangle with legs 3 and 4 is drawn; the right angle is marked and the sides "
                          "are labeled a, b and c.",
                "key_equation": None,
            },
            {
                "narration": "Now build a square on every side. The square on the leg a has area a squared, "
                             "which is nine. The square on b has area sixteen. And the square on the hypotenuse "
                             "has area c squared. So how are these three areas related?",
                "visual": "Three squares grow outward from the sides: blue on a, green on b, yellow on c, each "
                          "labeled with its area.",
                "key_equation": None,
            },
            {
                "narration": "The theorem says the two small squares together have exactly the same area as the "
                             "big one. Nine plus sixteen is twenty five, so the square on the hypotenuse has area "
                             "twenty five, and c must be five. You can even check it by counting the unit "
                             "squares inside each one.",
                "visual": "The equation a^2 + b^2 = c^2 is written, then the numbers 9 + 16 = 25 appear below "
                          "it and c = 5 is highlighted.",
                "key_equation": "a^2 + b^2 = c^2",
            },
            {
                "narration": "This works for every right triangle, which is why it is everywhere. Taking a square "
                             "root gives the length of the hypotenuse directly, and that is exactly the distance "
                             "formula you use to measure straight line distance on a grid. Physics, computer "
                             "graphics and machine learning all lean on it every day.",
                "visual": "The squares fade; the formula c = sqrt(a^2 + b^2) is shown and boxed.",
                "key_equation": r"c = \sqrt{a^2 + b^2}",
            },
        ],
    },
}

VECTORS_CODE = r'''from manim import *


class LessonScene(Scene):
    def sync_to(self, i):
        """Hold the last frame until narration segment i ends."""
        remaining = SEGMENT_ENDS[i] - self.renderer.time
        if remaining > 0.02:
            self.wait(remaining)

    def construct(self):
        # --- Segment 0: a vector is an arrow from the origin ---
        plane = NumberPlane(x_range=[-7, 7, 1], y_range=[-4, 4, 1],
                            background_line_style={"stroke_opacity": 0.35})
        origin = plane.c2p(0, 0)
        v = Arrow(origin, plane.c2p(3, 2), buff=0, color=YELLOW, stroke_width=6)
        v_label = MathTex(r"\vec{v}", color=YELLOW).next_to(v.get_end(), UR, buff=0.1)
        title = Text("Vectors as arrows", font_size=36).to_edge(UP).add_background_rectangle()
        self.play(Create(plane), FadeIn(title), run_time=2)
        self.play(GrowArrow(v), run_time=1.5)
        self.play(Write(v_label), run_time=1)
        self.sync_to(0)

        # --- Segment 1: coordinates ---
        self.play(FadeOut(title), run_time=0.5)
        drop_x = DashedLine(plane.c2p(3, 2), plane.c2p(3, 0), color=GREY_B)
        drop_y = DashedLine(plane.c2p(3, 2), plane.c2p(0, 2), color=GREY_B)
        brace_x = BraceBetweenPoints(origin, plane.c2p(3, 0), direction=DOWN, color=GREEN)
        three = MathTex("3", color=GREEN).next_to(brace_x, DOWN, buff=0.1)
        brace_y = BraceBetweenPoints(plane.c2p(3, 0), plane.c2p(3, 2), direction=RIGHT, color=RED)
        two = MathTex("2", color=RED).next_to(brace_y, RIGHT, buff=0.1)
        coords = MathTex(r"\vec{v} = \begin{bmatrix} 3 \\ 2 \end{bmatrix}").to_corner(UL)
        coords.add_background_rectangle()
        self.play(Create(drop_x), GrowFromCenter(brace_x), Write(three), run_time=2)
        self.play(Create(drop_y), GrowFromCenter(brace_y), Write(two), run_time=2)
        self.play(Write(coords), run_time=2)
        self.sync_to(1)

        # --- Segment 2: addition, tip to tail ---
        self.play(FadeOut(drop_x, drop_y, brace_x, brace_y, three, two, coords), run_time=0.8)
        w = Arrow(origin, plane.c2p(1, -3), buff=0, color=BLUE, stroke_width=6)
        w_label = MathTex(r"\vec{w}", color=BLUE).next_to(w.get_end(), DR, buff=0.1)
        self.play(GrowArrow(w), Write(w_label), run_time=1.5)
        w_moved = Arrow(plane.c2p(3, 2), plane.c2p(4, -1), buff=0, color=BLUE, stroke_width=6)
        self.play(ReplacementTransform(w, w_moved),
                  w_label.animate.next_to(plane.c2p(3.5, 0.5), RIGHT, buff=0.15), run_time=2)
        total = Arrow(origin, plane.c2p(4, -1), buff=0, color=GREEN, stroke_width=6)
        total_label = MathTex(r"\vec{v} + \vec{w}", color=GREEN).next_to(total.get_end(), DOWN, buff=0.15)
        self.play(GrowArrow(total), Write(total_label), run_time=1.5)
        sum_eq = MathTex(r"\vec{v} + \vec{w} = \begin{bmatrix} 4 \\ -1 \end{bmatrix}", color=GREEN)
        sum_eq.to_corner(UL).add_background_rectangle()
        self.play(Write(sum_eq), run_time=1.5)
        self.sync_to(2)

        # --- Segment 3: scaling ---
        self.play(FadeOut(w_moved, w_label, total, total_label, sum_eq), run_time=0.8)
        two_v = Arrow(origin, plane.c2p(6, 4), buff=0, color=YELLOW, stroke_width=6)
        two_v_label = MathTex(r"2\vec{v}", color=YELLOW).next_to(two_v.get_end(), LEFT, buff=0.15)
        self.play(ReplacementTransform(v, two_v), ReplacementTransform(v_label, two_v_label), run_time=2)
        neg_v = Arrow(origin, plane.c2p(-3, -2), buff=0, color=RED, stroke_width=6)
        neg_v_label = MathTex(r"-\vec{v}", color=RED).next_to(neg_v.get_end(), DL, buff=0.1)
        self.play(ReplacementTransform(two_v, neg_v), ReplacementTransform(two_v_label, neg_v_label),
                  run_time=2)
        closing = Text("add and scale", font_size=40).to_edge(DOWN).add_background_rectangle()
        self.play(FadeIn(closing, shift=UP), run_time=1)
        self.sync_to(3)
'''

PYTHAGORAS_CODE = r'''from manim import *
import numpy as np


class LessonScene(Scene):
    def sync_to(self, i):
        """Hold the last frame until narration segment i ends."""
        remaining = SEGMENT_ENDS[i] - self.renderer.time
        if remaining > 0.02:
            self.wait(remaining)

    def construct(self):
        u = 0.55  # scene units per triangle unit
        shift = np.array([-1.6, -0.9, 0])
        A = np.array([0, 0, 0]) * u + shift          # right angle
        B = np.array([3, 0, 0]) * u + shift          # end of leg a
        C = np.array([0, 4, 0]) * u + shift          # end of leg b

        # --- Segment 0: the triangle ---
        tri = Polygon(A, B, C, color=WHITE, stroke_width=5)
        corner = RightAngle(Line(A, B), Line(A, C), length=0.25, quadrant=(1, 1))
        a_lab = MathTex("a").next_to(Line(A, B), UP, buff=0.12)
        b_lab = MathTex("b").next_to(Line(A, C), RIGHT, buff=0.12)
        c_lab = MathTex("c").move_to((B + C) / 2 + np.array([-0.25, -0.2, 0]))
        title = Text("The Pythagorean theorem", font_size=36).to_edge(UP)
        self.play(Write(title), run_time=1.5)
        self.play(Create(tri), run_time=2)
        self.play(Create(corner), Write(a_lab), Write(b_lab), Write(c_lab), run_time=1.5)
        self.sync_to(0)

        # --- Segment 1: squares on every side ---
        sq_a = Polygon(A, B, B + np.array([0, -3, 0]) * u, A + np.array([0, -3, 0]) * u,
                       color=BLUE, fill_opacity=0.45)
        sq_b = Polygon(A, C, C + np.array([-4, 0, 0]) * u, A + np.array([-4, 0, 0]) * u,
                       color=GREEN, fill_opacity=0.45)
        out = np.array([4, 3, 0]) * u  # outward normal of the hypotenuse, length c
        sq_c = Polygon(B, C, C + out, B + out, color=YELLOW, fill_opacity=0.35)
        area_a = MathTex("9").move_to(sq_a.get_center())
        area_b = MathTex("16").move_to(sq_b.get_center())
        area_c = MathTex("c^2").move_to(sq_c.get_center())
        self.play(FadeOut(title), DrawBorderThenFill(sq_a), run_time=1.5)
        self.play(Write(area_a), run_time=0.7)
        self.play(DrawBorderThenFill(sq_b), run_time=1.5)
        self.play(Write(area_b), run_time=0.7)
        self.play(DrawBorderThenFill(sq_c), Write(area_c), run_time=1.5)
        self.sync_to(1)

        # --- Segment 2: the theorem, with numbers ---
        eq = MathTex("a^2", "+", "b^2", "=", "c^2").scale(1.2).to_corner(UR).shift(LEFT * 0.4)
        eq[0].set_color(BLUE)
        eq[2].set_color(GREEN)
        eq[4].set_color(YELLOW)
        nums = MathTex("9", "+", "16", "=", "25").scale(1.2).next_to(eq, DOWN, buff=0.4)
        c_val = MathTex("c = 5", color=YELLOW).scale(1.2).next_to(nums, DOWN, buff=0.4)
        self.play(Write(eq), run_time=2)
        self.play(TransformFromCopy(area_a, nums[0]), TransformFromCopy(area_b, nums[2]),
                  FadeIn(nums[1]), FadeIn(nums[3]), run_time=2)
        self.play(Write(nums[4]), Transform(area_c, MathTex("25").move_to(sq_c.get_center())), run_time=1)
        self.play(Write(c_val), Indicate(c_lab), run_time=1.5)
        self.sync_to(2)

        # --- Segment 3: general formula / distance ---
        self.play(FadeOut(sq_a, sq_b, sq_c, area_a, area_b, area_c, nums, c_val), run_time=1)
        formula = MathTex(r"c = \sqrt{a^2 + b^2}").scale(1.3).next_to(eq, DOWN, buff=0.6)
        box = SurroundingRectangle(formula, color=YELLOW, buff=0.2)
        self.play(Write(formula), run_time=2)
        self.play(Create(box), run_time=1)
        dist = Text("distance on a grid", font_size=30).next_to(box, DOWN, buff=0.3)
        self.play(FadeIn(dist, shift=UP), run_time=1)
        self.sync_to(3)
'''

CODE = {
    "Vectors as arrows": VECTORS_CODE,
    "The Pythagorean theorem": PYTHAGORAS_CODE,
}

# What a model typically gets wrong first: a manimgl-era API name. Used to
# show the self-repair loop working on a real render failure.
BROKEN_FIRST_TRY = {
    "Vectors as arrows": VECTORS_CODE.replace("self.play(Create(plane), FadeIn(title), run_time=2)",
                                              "self.play(ShowCreation(plane), FadeIn(title), run_time=2)"),
}
