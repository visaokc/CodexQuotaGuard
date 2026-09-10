"""Application-local supersampled surfaces; never patch the installed widget library."""
from functools import lru_cache
import ctypes
import os
from tkinter import font as tkfont

import customtkinter as native
from PIL import Image, ImageDraw, ImageTk


def __getattr__(name):
    return getattr(native, name)


@lru_cache(maxsize=128)
def surface(width, height, radius, border, background, foreground, outline, corners, split):
    scale = 3
    w, h = width*scale, height*scale
    image = Image.new('RGB', (w, h), background)
    draw = ImageDraw.Draw(image)
    if corners:
        for box, color in zip(((0, 0, w//2, h//2), (w//2, 0, w, h//2),
                               (w//2, h//2, w, h), (0, h//2, w//2, h)), corners):
            draw.rectangle(box, fill=color)
    radius, border = radius*scale, border*scale
    draw.rounded_rectangle((0, 0, w-1, h-1), radius=radius,
                           fill=outline if border else foreground)
    if border and w > 2*border and h > 2*border:
        draw.rounded_rectangle((border, border, w-1-border, h-1-border),
                               radius=max(0, radius-border), fill=foreground)
    if split:
        mask = Image.new('L', (w, h))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w-1, h-1), radius=radius, fill=255)
        ImageDraw.Draw(mask).rectangle((0, 0, max(0, w-height*scale), h), fill=0)
        image.paste(split, (0, 0, w, h), mask)
    return image.resize((width, height), Image.Resampling.LANCZOS)


class Surface:
    def configure(self, require_redraw=False, **kwargs):
        changed = {}
        for key, value in kwargs.items():
            try:
                if self.cget(key) == value:
                    continue
            except (ValueError, AttributeError):
                pass
            changed[key] = value
        if changed or require_redraw:
            super().configure(require_redraw=require_redraw, **changed)

    def _draw(self, no_color_updates=False):
        super()._draw(no_color_updates)
        self._surface()

    def _surface(self):
        if not hasattr(self, '_canvas') or not self._canvas.winfo_exists():
            return
        radius = round(self._apply_widget_scaling(getattr(self, '_corner_radius', 0)))
        border = round(self._apply_widget_scaling(getattr(self, '_border_width', 0)))
        if not radius and not border:
            return
        width = max(1, round(self._apply_widget_scaling(self._current_width)))
        height = max(1, round(self._apply_widget_scaling(self._current_height)))
        def color(value):
            value = self._apply_appearance_mode(value)
            return '#' + ''.join(f'{v//257:02x}' for v in self.winfo_rgb(value))
        bg = color(self._bg_color)
        fg = color(self._fg_color) if self._fg_color != 'transparent' else bg
        corners = getattr(self, '_background_corner_colors', None)
        key = (width, height, min(radius, width//2, height//2), border, bg, fg,
               color(getattr(self, '_border_color', bg)),
               tuple(color(c) for c in corners) if corners else (),
               color(self._button_color) if hasattr(self, '_button_color') else None)
        if getattr(self, '_surface_key', None) != key:
            self._surface_key = key
            self._surface_image = ImageTk.PhotoImage(surface(*key), master=self._canvas)
            self._canvas.delete('aa_surface')
            self._canvas.create_image(0, 0, anchor='nw', image=self._surface_image, tags='aa_surface')
        self._canvas.tag_raise('aa_surface')
        self._canvas.tag_raise('dropdown_arrow')


class CTkFrame(Surface, native.CTkFrame):
    pass


class CTkLabel(Surface, native.CTkLabel):
    pass


class CTkEntry(Surface, native.CTkEntry):
    pass


class CTkOptionMenu(Surface, native.CTkOptionMenu):
    def __init__(self, *args, **kwargs):
        font = kwargs.get('font', ('Microsoft YaHei UI', 12))
        if isinstance(font, tuple):
            font = (font[0], max(12, font[1]), *font[2:])
        kwargs.update(font=font, dropdown_font=('Microsoft YaHei UI', 13), dynamic_resizing=False)
        kwargs.setdefault('dropdown_fg_color', '#22262d')
        kwargs.setdefault('dropdown_hover_color', '#31496b')
        super().__init__(*args, **kwargs)
        self.popup = None

    def _open_dropdown_menu(self):
        if self.popup and self.popup.winfo_exists():
            self._dismiss_popup()
            return
        popup = self.popup = native.CTkToplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(fg_color='#22262d')
        popup.attributes('-topmost', True)
        font = tkfont.Font(root=self, font=self._apply_font_scaling(('Microsoft YaHei UI', 13)))
        width = max(170, round(self._reverse_widget_scaling(self.winfo_width())),
                    round(self._reverse_widget_scaling(max((font.measure(v) for v in self._values), default=0)))+32)
        height = min(8, len(self._values))*34+12
        x, y = self.winfo_rootx(), self.winfo_rooty()+self.winfo_height()+5
        x = max(0, min(x, self.winfo_screenwidth()-round(self._apply_widget_scaling(width))-8))
        physical_height = self._apply_widget_scaling(height)
        if y+physical_height > self.winfo_screenheight():
            y = self.winfo_rooty()-round(physical_height)-5
        popup.geometry(f'{width}x{height}+{x}+{y}')
        panel = native.CTkScrollableFrame(popup, fg_color='#22262d', corner_radius=10) if len(self._values) > 8 else CTkFrame(popup, fg_color='#22262d', corner_radius=10)
        panel.pack(fill='both', expand=True, padx=5, pady=5)
        buttons = []
        def dismiss():
            if popup.winfo_exists():
                if getattr(popup, '_menu_fade', None):
                    popup.after_cancel(popup._menu_fade)
                popup.destroy()
            self.popup = None
        self._dismiss_popup = dismiss
        def choose(value):
            dismiss()
            self._dropdown_callback(value)
        for value in self._values:
            item = CTkButton(panel, text=value, height=32, anchor='w', corner_radius=7,
                fg_color='#31496b' if value == self.get() else '#22262d', hover_color='#3a5070',
                font=('Microsoft YaHei UI', 13), command=lambda v=value: choose(v))
            item.pack(fill='x', pady=1)
            buttons.append(item)
        selected = [self._values.index(self.get()) if self.get() in self._values else 0]
        def move(step):
            selected[0] = max(0, min(len(buttons)-1, selected[0]+step))
            for i, item in enumerate(buttons):
                item.configure(fg_color='#31496b' if i == selected[0] else '#22262d')
        def outside(event):
            if not (popup.winfo_rootx() <= event.x_root <= popup.winfo_rootx()+popup.winfo_width()
                    and popup.winfo_rooty() <= event.y_root <= popup.winfo_rooty()+popup.winfo_height()):
                dismiss()
        popup.bind('<Button-1>', outside)
        popup.bind('<Escape>', lambda _: dismiss())
        def focus_out(_):
            def check():
                if popup.winfo_exists():
                    focused = popup.focus_get()
                    if focused is None or focused.winfo_toplevel() is not popup:
                        dismiss()
            popup.after_idle(check)
        popup.bind('<FocusOut>', focus_out)
        popup.bind('<Down>', lambda _: move(1))
        popup.bind('<Up>', lambda _: move(-1))
        popup.bind('<Return>', lambda _: choose(self._values[selected[0]]))
        popup.attributes('-alpha', .35)
        interactive = True
        if os.name == 'nt':
            popup.update_idletasks()
            user = ctypes.windll.user32
            interactive = bool(user.IsWindowEnabled(user.GetParent(self.winfo_toplevel().winfo_id())))
            if not interactive:
                hwnd = user.GetParent(popup.winfo_id())
                user.SetWindowLongW(hwnd, -20, user.GetWindowLongW(hwnd, -20) | 0x08000020)
        popup.deiconify()
        popup.update_idletasks()
        if os.name == 'nt':
            hwnd = ctypes.windll.user32.GetParent(popup.winfo_id())
            corner = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
        if interactive:
            popup.focus_set()
            popup.grab_set()
        def fade(frame=1):
            popup._menu_fade = None
            if popup.winfo_exists():
                popup.attributes('-alpha', .35+.65*frame/6)
                if frame < 6:
                    popup._menu_fade = popup.after(16, lambda: fade(frame+1))
        fade()

    def destroy(self):
        if getattr(self, 'popup', None) and self.popup.winfo_exists():
            self._dismiss_popup()
        super().destroy()


class CTkButton(Surface, native.CTkButton):
    """Short color easing for hover and selected navigation/filter states."""
    def __init__(self, *args, **kwargs):
        self._fade_job, self._visual_color = None, None
        super().__init__(*args, **kwargs)
        self._visual_color = self._apply_appearance_mode(self._fg_color)

    def _create_grid(self):
        super()._create_grid()
        if self._image_label is None and self._text_label is not None and self._anchor == 'center':
            # A single centered cell avoids the empty image column shifting text.
            self.grid_columnconfigure((1, 3), weight=0)
            self.grid_columnconfigure(2, weight=0)
            self._text_label.grid(row=2, column=2, sticky='')

    def _animate_color(self, target):
        target = self._apply_appearance_mode(target)
        if target == 'transparent':
            target = self._apply_appearance_mode(self._bg_color)
        source = self._visual_color or target
        if source == 'transparent':
            source = self._apply_appearance_mode(self._bg_color)
        if self._fade_job:
            self.after_cancel(self._fade_job)
        rgb = lambda value: self.winfo_rgb(value)
        a, b = rgb(source), rgb(target)
        if a == b:
            self._fade_job = None
            return
        def tick(frame=1):
            self._fade_job = None
            t = 1-(1-frame/8)**3
            self._visual_color = '#' + ''.join(f'{round((x+(y-x)*t)/257):02x}' for x, y in zip(a, b))
            original = self._fg_color
            self._fg_color = self._visual_color
            self._draw()
            self._fg_color = original
            if frame < 8:
                self._fade_job = self.after(15, lambda: tick(frame+1))
        tick()

    def configure(self, require_redraw=False, **kwargs):
        changed = 'fg_color' in kwargs and kwargs['fg_color'] != self._fg_color
        super().configure(require_redraw=require_redraw, **kwargs)
        if changed and self._visual_color is not None:
            self._animate_color(self._fg_color)

    def _on_enter(self, event=None):
        if self._state != 'disabled' and self._hover:
            self._animate_color(self._hover_color)

    def _on_leave(self, event=None):
        self._animate_color(self._fg_color)

    def destroy(self):
        if self._fade_job:
            self.after_cancel(self._fade_job)
        super().destroy()


class CTkSegmentedButton(native.CTkSegmentedButton):
    def _create_button(self, index, value):
        return CTkButton(self, width=0, height=self._current_height,
            corner_radius=self._sb_corner_radius, border_width=self._sb_border_width,
            fg_color=self._sb_unselected_color, border_color=self._sb_fg_color,
            hover_color=self._sb_unselected_hover_color, text_color=self._sb_text_color,
            text_color_disabled=self._sb_text_color_disabled, text=value, font=self._font,
            state=self._state, command=lambda: self.set(value, from_button_callback=True),
            background_corner_colors=None, round_width_to_even_numbers=False,
            round_height_to_even_numbers=False)
