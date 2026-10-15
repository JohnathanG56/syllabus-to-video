from manim import *

# --- injected by the pipeline (narration timing, seconds) ---
SEGMENT_DURATIONS = [6.0, 7.5, 6.5]
SEGMENT_ENDS = [6.0, 13.5, 20.0]
# ------------------------------------------------------------


class LessonScene(Scene):
    def sync_to(self, i):
        """Hold the last frame until narration segment i ends."""
        remaining = SEGMENT_ENDS[i] - self.renderer.time
        if remaining > 0.02:
            self.wait(remaining)

    def construct(self):
        # --- Segment 0: a vector is an arrow from the origin (6.0s) ---
        plane = NumberPlane(x_range=[-7, 7, 1], y_range=[-4, 4, 1],
                            background_line_style={"stroke_opacity": 0.4})
        v = Arrow(plane.c2p(0, 0), plane.c2p(3, 1), buff=0, color=YELLOW)
        v_label = MathTex(r"\vec{v}", color=YELLOW).next_to(v.get_end(), UR, buff=0.1)
        self.play(Create(plane), run_time=1.5)
        self.play(GrowArrow(v), Write(v_label), run_time=1.5)
        self.sync_to(0)

        # --- Segment 1: add w by placing it tip to tail (7.5s) ---
        w = Arrow(plane.c2p(0, 0), plane.c2p(1, 2), buff=0, color=BLUE)
        w_label = MathTex(r"\vec{w}", color=BLUE).next_to(w.get_end(), LEFT, buff=0.1)
        self.play(GrowArrow(w), Write(w_label), run_time=1.5)
        w_moved = Arrow(plane.c2p(3, 1), plane.c2p(4, 3), buff=0, color=BLUE)
        self.play(ReplacementTransform(w, w_moved), w_label.animate.next_to(w_moved.get_end(), UP, buff=0.1),
                  run_time=2)
        self.sync_to(1)

        # --- Segment 2: the sum goes from the origin to the final tip (6.5s) ---
        s = Arrow(plane.c2p(0, 0), plane.c2p(4, 3), buff=0, color=GREEN)
        eq = MathTex(r"\vec{v} + \vec{w}", color=GREEN).to_corner(UL).add_background_rectangle()
        self.play(GrowArrow(s), run_time=1.5)
        self.play(Write(eq), Indicate(s), run_time=2)
        self.sync_to(2)
