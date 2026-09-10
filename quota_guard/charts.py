"""Small native Canvas charts with bounded, cancellable interaction animations."""
import math
import time
import tkinter as tk
from tkinter import font as tkfont
from datetime import datetime
from PIL import Image, ImageDraw, ImageTk

PANEL, FG, MUTED = '#181a1d', '#eef0f4', '#89909c'
COLORS = ('#669cff', '#f6b763', '#55d6be', '#cd8af0', '#ed8299', '#c6db76')


def blend(a, b, amount):
    return '#' + ''.join(f'{round(int(a[i:i+2], 16)*(1-amount)+int(b[i:i+2], 16)*amount):02x}'
                        for i in (1, 3, 5))


def compact(value):
    if abs(value) >= 1e6:
        return f'{value / 1e6:.2f}M'
    if abs(value) >= 1000:
        return f'{value / 1000:.1f}k'
    return f'{value:.1f}'.rstrip('0').rstrip('.')


def axis_scale(maximum):
    """Four evenly spaced, readable intervals with headroom above the data."""
    raw = max(maximum, 1) / 4
    power = 10 ** math.floor(math.log10(raw))
    step = next(n for n in (1, 2, 2.5, 5, 10) if n*power >= raw) * power
    return step * 4


def smooth_points(values, subdivisions=12):
    """Monotone cubic interpolation: pass every sample, never overshoot an interval."""
    if len(values) < 2:
        return list(enumerate(values))
    slopes = [b-a for a, b in zip(values, values[1:])]
    tangents = [slopes[0]]
    for a, b in zip(slopes, slopes[1:]):
        tangents.append(2*a*b/(a+b) if a*b > 0 else 0)
    tangents.append(slopes[-1])
    points = []
    for i, (a, b) in enumerate(zip(values, values[1:])):
        for j in range(subdivisions):
            t = j/subdivisions
            y = ((2*t**3-3*t**2+1)*a + (t**3-2*t**2+t)*tangents[i]
                 + (-2*t**3+3*t**2)*b + (t**3-t**2)*tangents[i+1])
            points.append((i+t, max(min(a, b), min(max(a, b), y))))
    points.append((len(values)-1, values[-1]))
    return points


class Chart(tk.Canvas):
    def __init__(self, parent, kind, **kwargs):
        super().__init__(parent, bg=PANEL, highlightthickness=0, **kwargs)
        self.kind, self.values, self.labels = kind, [], []
        self.colors = COLORS
        self.label_font = tkfont.Font(root=self, family='Microsoft YaHei UI', size=9)
        self.target, self.key, self.job = [], None, None
        self.reveal, self.raster = 1, None
        self.hover, self.start, self.step, self.unit = None, 0, 1, 'Token'
        self.hover_job = None
        self.hover_position = self.hover_target = 0.0
        self.hover_alpha = self.hover_opacity = 0.0
        self.draw_size = None
        self.detail_callback = None
        self.bind('<Configure>', self.resized)
        self.bind('<Motion>', self.motion)
        self.bind('<Leave>', self.leave)
        self.bind('<Destroy>', self.destroyed)

    def resized(self, event):
        size = (event.width, event.height)
        if size != self.draw_size:
            self.draw_size = size
            self.draw()

    def destroyed(self, event):
        if event.widget is self and self.job:
            self.after_cancel(self.job)
            self.job = None
        if event.widget is self and self.hover_job:
            self.after_cancel(self.hover_job)
            self.hover_job = None

    def set_data(self, values, labels=(), start=0, step=1, unit='Token', colors=()):
        key = (tuple(values), tuple(labels), start, step, unit, tuple(colors))
        if key == self.key:
            return
        self.key = key
        self.colors = tuple(colors) or COLORS
        self.labels, self.start, self.step, self.unit = list(labels), start, step, unit
        if self.hover is not None and self.hover >= len(values):
            self.hover = None
            self.hover_opacity = 0
        self.hover_target = min(self.hover_target, max(0, len(values)-1))
        if self.job:
            self.after_cancel(self.job)
        previous = self.values if len(self.values) == len(values) else [0] * len(values)
        reveal = not any(previous)
        self.target = list(values)
        began = time.monotonic()

        def tick():
            self.job = None
            t = min(1, (time.monotonic() - began) / .22)
            eased = 1 - (1 - t) ** 3
            self.reveal = eased if reveal else 1
            self.values = [a + (b - a) * eased for a, b in zip(previous, self.target)]
            self.draw()
            if t < 1:
                self.job = self.after(16, tick)
        tick()

    def leave(self, _=None):
        self.hover_opacity = 0
        self.animate_hover()

    def animate_hover(self):
        if self.hover_job:
            return
        def tick():
            self.hover_job = None
            self.hover_position += (self.hover_target-self.hover_position)*.32
            self.hover_alpha += (self.hover_opacity-self.hover_alpha)*.32
            if self.hover_opacity == 0 and self.hover_alpha < .015:
                self.hover = None
                self.hover_alpha = 0
            self.draw()
            if (abs(self.hover_position-self.hover_target) > .003
                    or abs(self.hover_alpha-self.hover_opacity) > .003):
                self.hover_job = self.after(16, tick)
        tick()

    def motion(self, event):
        w, h = self.winfo_width(), self.winfo_height()
        if self.kind == 'pie':
            cx, cy, radius = self.pie_geometry(h)
            dx, dy = event.x - cx, cy - event.y
            selected = None
            if radius * .62 <= math.hypot(dx, dy) <= radius + 4 and sum(self.target):
                angle = (math.degrees(math.atan2(dy, dx)) - 90) % 360
                end = 0
                for i, value in enumerate(self.target):
                    end += value / sum(self.target) * 360
                    if angle <= end:
                        selected = i
                        break
            if event.x >= 153:
                offset, spacing = self.legend_geometry(h)
                index = int((event.y-offset)/spacing) if event.y >= offset else -1
                if 0 <= index < len(self.labels):
                    selected = index
            self.hover = selected
            self.hover_target = selected or 0
            self.hover_opacity = 1 if selected is not None else 0
        elif self.values and 52 <= event.x <= w - 12 and 25 <= event.y <= h - 31:
            position = (event.x-52)/max(1, w-64)*len(self.values)-.5
            self.hover = max(0, min(len(self.values) - 1,
                round(position)))
            target = max(0, min(len(self.values)-1, position))
            if not self.hover_alpha:
                self.hover_position = target
            self.hover_target = target
            self.hover_opacity = 1
        else:
            self.hover_opacity = 0
        self.animate_hover()

    def draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 5 or h < 5:
            return
        if self.kind == 'pie':
            self.pie(w, h)
        else:
            self.line(w, h)

    def pie_geometry(self, h):
        available = h-30
        return 79, available/2+3, min(62, available/2-5)

    def legend_geometry(self, h):
        spacing = min(50, max(30, (h-36)/max(1, len(self.labels))))
        return max(5, (h-30-((len(self.labels)-1)*spacing+30))/2+3), spacing

    def pie(self, w, h):
        cx, cy, r = self.pie_geometry(h)
        total = sum(self.values)
        scale = 3
        raster = Image.new('RGB', (w*scale, h*scale), PANEL)
        draw = ImageDraw.Draw(raster)
        box = lambda radius: tuple(round(v*scale) for v in (cx-radius, cy-radius, cx+radius, cy+radius))
        draw.ellipse(box(r), fill='#2a2e35')
        angle = 90
        for i, value in enumerate(self.values):
            extent = value / total * 360 * self.reveal if total else 0
            radius = r + (3*self.hover_alpha if i == self.hover else 0)
            if extent:
                draw.pieslice(box(radius), -angle-extent, -angle, fill=self.colors[i % len(self.colors)])
            angle += extent
        draw.ellipse(box(r*.65), fill=PANEL)
        self.raster = ImageTk.PhotoImage(raster.resize((w, h), Image.Resampling.LANCZOS), master=self)
        self.create_image(0, 0, image=self.raster, anchor='nw')
        if self.detail_callback:
            self.detail_callback(f'{compact(self.target[self.hover])} Token'
                                 if self.hover is not None and self.hover_opacity else '')
        offset, spacing = self.legend_geometry(h)
        for i, label in enumerate(self.labels):
            y = offset + i * spacing
            value = self.target[i]
            shown = label
            while len(shown) > 1 and self.label_font.measure(shown) > w-178:
                shown = shown[:-2]+'…'
            self.create_oval(150, y+5, 155, y+10, fill=self.colors[i % len(self.colors)], outline='')
            self.create_text(163, y, anchor='nw', text=shown, fill=FG, font=self.label_font)
            self.create_text(163, y+17, anchor='nw', text=compact(value)+' Token',
                             fill=MUTED, font=('Segoe UI', 9))
        if not self.labels:
            self.create_text(157, cy, anchor='w', text='等待已同步日志', fill=MUTED, font=('Microsoft YaHei UI', 9))

    def line(self, w, h):
        left, right, top, bottom = 52, w-12, 25, h-31
        peak = axis_scale(max(self.target, default=0))
        scale = 3
        raster = Image.new('RGB', (w*scale, h*scale), PANEL)
        draw = ImageDraw.Draw(raster)
        series_color = self.colors[0]
        curve = [(left+(right-left)*(x+.5)/max(1, len(self.values)), bottom-y/peak*(bottom-top))
                 for x, y in smooth_points(self.values)]
        scaled = lambda points: [(round(x*scale), round(y*scale)) for x, y in points]
        if len(curve) >= 2 and self.kind == 'line':
            draw.polygon(scaled([(left, bottom)] + curve + [(right, bottom)]), fill=blend(PANEL, series_color, .16))
        for i in range(5):
            y = bottom - (bottom-top)*i/4
            for x in range(left, right, 6):
                draw.line(scaled([(x, y), (min(x+2, right), y)]), fill='#353b46', width=scale)
        if len(curve) >= 2 and self.kind == 'line':
            draw.line(scaled(curve), fill=series_color, width=2*scale, joint='curve')
        if self.kind == 'bar':
            span = (right-left)/max(1, len(self.values))
            for i, value in enumerate(self.values):
                if value <= 0:
                    continue
                x = left+(i+.5)*span
                y = bottom-value/peak*(bottom-top)
                intensity = max(0, 1-abs(i-self.hover_position))*self.hover_alpha
                if intensity > .01:
                    draw.rounded_rectangle(scaled([(x-span*.48, top), (x+span*.48, bottom+2)]),
                        radius=3*scale, fill=blend(PANEL, series_color, intensity*.2))
                color = blend(series_color, '#ffffff', intensity*.5)
                draw.rounded_rectangle(scaled([(x-span*.34, y), (x+span*.34, bottom)]),
                    radius=min(3*scale, (bottom-y)*scale/2), fill=color)
                # A restrained highlight cap gives columns a distinct visual response.
                if bottom-y > 2:
                    draw.line(scaled([(x-span*.22, y+1), (x+span*.22, y+1)]),
                              fill=blend(series_color, '#ffffff', .6), width=scale)
        if self.kind == 'line' and self.hover is not None and self.hover_alpha > .01 and curve:
            position = min(len(curve)-1, self.hover_position*12)
            index = int(position)
            a, b = curve[index], curve[min(index+1, len(curve)-1)]
            x, y = (a[j]+(b[j]-a[j])*(position-index) for j in (0, 1))
            intensity = self.hover_alpha
            draw.line(scaled([(x, top), (x, bottom)]), fill=blend(PANEL, series_color, .4), width=scale)
            for radius, color in [(8, blend(PANEL, series_color, .3)), (4, blend(series_color, '#ffffff', .4)), (2, '#edf5ff')]:
                radius *= intensity
                draw.ellipse(scaled([(x-radius,y-radius),(x+radius,y+radius)]),fill=color)
        self.raster = ImageTk.PhotoImage(raster.resize((w, h), Image.Resampling.LANCZOS), master=self)
        self.create_image(0, 0, image=self.raster, anchor='nw')
        self.create_text(8, 8, anchor='w', text=self.unit, fill=MUTED, font=('Segoe UI', 8))
        for i in range(5):
            self.create_text(left-7, bottom-(bottom-top)*i/4, anchor='e', text=compact(peak*i/4),
                             fill=MUTED, font=('Segoe UI', 8))
        coords = []
        for i, value in enumerate(self.values):
            x = left+(right-left)*(i+.5)/len(self.values)
            coords.extend((x, bottom - value/peak*(bottom-top)))
        if not any(self.target):
            self.create_text((left+right)/2, (top+bottom)/2, text='所选时段暂无已同步用量',
                             fill=MUTED, font=('Microsoft YaHei UI', 10))
        for fraction in (0, .5, 1):
            timestamp = self.start + self.step * len(self.values) * fraction
            label = datetime.fromtimestamp(timestamp).strftime('%m/%d %H:%M' if self.step <= 3600 else '%m/%d')
            self.create_text(left+(right-left)*fraction, h-14, text=label,
                             anchor='w' if fraction == 0 else 'e' if fraction == 1 else 'center',
                             fill=MUTED, font=('Segoe UI', 8))
        if self.hover is not None and coords and self.hover_alpha > .05:
            i = max(0, min(len(self.target)-1, round(self.hover_position)))
            text = datetime.fromtimestamp(self.start+i*self.step).strftime('%m/%d %H:%M')
            text += f' · {compact(self.target[i])} {self.unit}'
            self.create_rectangle(left, 0, right, 17, fill=PANEL, outline='')
            self.create_text(right, 7, text=text, anchor='e', fill=FG, font=('Segoe UI', 9))
