from manim import *

# --- injected by the pipeline (narration timing, seconds) ---
SEGMENT_DURATIONS = [5.0, 8.0, 6.0]
SEGMENT_ENDS = [5.0, 13.0, 19.0]
# ------------------------------------------------------------


class LessonScene(Scene):
    def sync_to(self, i):
        """Hold the last frame until narration segment i ends."""
        remaining = SEGMENT_ENDS[i] - self.renderer.time
        if remaining > 0.02:
            self.wait(remaining)

    def construct(self):
        # --- Segment 0: the graph of f(x) = x^2 (5.0s) ---
        title = Text("Slope of a tangent line", font_size=36).to_edge(UP)
        axes = Axes(x_range=[-3, 3, 1], y_range=[0, 9, 1], x_length=7, y_length=4.5).shift(DOWN * 0.5)
        graph = axes.plot(lambda x: x ** 2, x_range=[-3, 3], color=BLUE)
        f_label = MathTex(r"f(x) = x^2", color=BLUE).next_to(axes, RIGHT, buff=0.2).shift(UP)
        self.play(Write(title), Create(axes), run_time=1.5)
        self.play(Create(graph), FadeIn(f_label), run_time=1.5)
        self.sync_to(0)

        # --- Segment 1: a tangent line slides along the curve (8.0s) ---
        x = ValueTracker(-2)
        dot = always_redraw(lambda: Dot(axes.c2p(x.get_value(), x.get_value() ** 2), color=YELLOW))
        tangent = always_redraw(lambda: axes.get_secant_slope_group(
            x.get_value(), graph, dx=0.01, secant_line_length=3, secant_line_color=YELLOW).submobjects[-1])
        self.play(FadeIn(dot), Create(tangent), run_time=1)
        self.play(x.animate.set_value(2), run_time=4, rate_func=smooth)
        self.sync_to(1)

        # --- Segment 2: the slope is the derivative (6.0s) ---
        deriv = MathTex(r"f'(x) = 2x", color=YELLOW).next_to(f_label, DOWN, aligned_edge=LEFT)
        box = SurroundingRectangle(deriv, color=YELLOW, buff=0.15)
        self.play(Write(deriv), run_time=1.5)
        self.play(Create(box), run_time=1)
        self.sync_to(2)
