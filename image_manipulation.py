"""
pyvips port of https://github.com/esmBot/esmBot/tree/master/natives/image
Copyright (C) 2026 Joseph Winkie

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import math
import os
import io
import asyncio
import time

import pyvips
import qrcode
from pyzbar.pyzbar import decode
from PIL import Image


class EsmBotImageProcessor:
    def __init__(self, base_path: str = "./", max_threads: int = 2, max_cache_mem: int = 1024 * 1024 * 1024,
                 timeout_seconds: int = 15):
        pyvips.concurrency_set(max_threads)
        pyvips.cache_set_max_mem(max_cache_mem)
        pyvips.cache_set_max(500)

        self.base_path = base_path
        self.timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()

        # Creates a 3x3 matrix image for the recomb operation
        self._sepia_matrix = pyvips.Image.new_from_array([
            [0.3588, 0.7044, 0.1368],
            [0.2990, 0.5870, 0.1140],
            [0.2392, 0.4696, 0.0912]
        ])

    def _timeout_callback(self, image: pyvips.Image, progress, start_time: float):
        """GLib signal callback to kill runaway libvips pipelines."""
        if time.time() - start_time > self.timeout_seconds:
            image.set_kill(True)

    def _load_image(self, buffer_data: bytes) -> pyvips.Image:
        """Safely loads all frames for animations, or a single frame for static images."""
        try:
            # Tell libvips to load every page/frame into a single tall vertical strip
            return pyvips.Image.new_from_buffer(buffer_data, "", n=-1)
        except pyvips.Error:
            # Fallback for static image formats (JPEG/PNG) that reject the 'n' parameter
            return pyvips.Image.new_from_buffer(buffer_data, "")

    def _process_invert(self, buffer_data: bytes) -> bytes:
        """Synchronous pyvips pipeline for inverting colors."""
        img = self._load_image(buffer_data)

        if img.hasalpha():
            color_bands = img.extract_band(0, n=img.bands - 1)
            alpha_band = img.extract_band(img.bands - 1)
            inverted_colors = color_bands.invert()
            out_img = inverted_colors.bandjoin(alpha_band)
        else:
            out_img = img.invert()

        out_img.set_progress(True)

        # Capture the start time in the local scope
        start_time = time.time()

        # Define the callback locally so it can access start_time
        def timeout_callback(image, progress):
            if time.time() - start_time > self.timeout_seconds:
                image.set_kill(True)

        # Pass only the signal name and the callback function
        out_img.signal_connect("eval", timeout_callback)

        return out_img.write_to_buffer(".webp[Q=80]")

    async def invert(self, buffer_data: bytes) -> bytes:
        """Async wrapper to be called directly from your slixmpp event handlers."""
        async with self._lock:
            # Offload the blocking libvips C-bindings to a background thread
            # so the XMPP event loop remains responsive
            return await asyncio.to_thread(self._process_invert, buffer_data)

    def _finalize_image(self, image: pyvips.Image, format_str: str = ".webp[Q=80]") -> bytes:
        """Helper to attach the GObject signal and return the memory buffer."""
        start_time = time.time()

        def timeout_callback(img, progress):
            if time.time() - start_time > self.timeout_seconds:
                img.set_kill(True)

        image.set_progress(True)
        image.signal_connect("eval", timeout_callback)
        return image.write_to_buffer(format_str)

    def _get_page_data(self, img: pyvips.Image):
        """Helper to extract animation metadata safely."""
        fields = img.get_fields()
        page_height = img.get("page-height") if "page-height" in fields else img.height
        n_pages = img.get("n-pages") if "n-pages" in fields else 1
        return img.width, page_height, n_pages

    def _process_blur(self, buffer_data: bytes, sharp: bool) -> bytes:
        img = self._load_image(buffer_data)
        # Applies sharpen with a sigma of 3 if sharp is true, otherwise applies a gaussian blur with a sigma of 5
        out = img.sharpen(sigma=3) if sharp else img.gaussblur(5)
        return self._finalize_image(out)

    async def blur(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_blur, buffer_data, False)

    async def sharpen(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_blur, buffer_data, True)

    def _process_bounce(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data).colourspace(
            "srgb")  # Converts the colorspace to sRGB
        if not img.hasalpha(): img = img.bandjoin(255)  # Adds an alpha channel if one does not exist

        width, page_height, n_pages = self._get_page_data(img)
        multi_page = n_pages > 1

        if n_pages == 1:
            multi_page = False
            n_pages = 15  # Defaults to 15 frames if the input is a static image

        mult = 3.14 / n_pages  # Calculates the sine wave multiplier using pi
        half_height = page_height // 2  # Calculates half the height to determine the canvas expansion

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if multi_page else img
            # Calculates the vertical offset using the sine function
            height_offset = int(half_height * (-math.sin(i * mult) + 1))
            # Embeds the frame into a larger canvas pushed down by the offset
            embedded = frame.embed(0, height_offset, width, page_height + half_height)
            frames.append(embedded)

        final = pyvips.Image.arrayjoin(frames, across=1)
        # Updates the metadata page height
        final.set_type(
            pyvips.GValue.gint_type,
            "page-height",
            page_height + half_height
        )
        if not multi_page:
            final.set_type(
                # Sets a frame delay of 50 if the original was static
                pyvips.GValue.array_int_type,
                "delay",
                [50] * 15
            )

        return self._finalize_image(final)

    async def bounce(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_bounce, buffer_data)

    def _process_caption(self, buffer_data: bytes, caption_text: str) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)  # Adds alpha if missing before captioning

        width, page_height, n_pages = self._get_page_data(img)
        size = width // 10  # Calculates the text size as one-tenth of the image width
        text_width = width - ((width // 25) * 2)  # Calculates padding by subtracting fractions of the width

        # Formats the text with a white background span
        markup = f'<span background="white">{caption_text}</span>'
        font_string = f"futura bold {size}"  # Simplified fallback

        # Generates the text image aligned to the center
        text = pyvips.Image.text(
            markup,
            rgba=True,
            align="centre",
            font=font_string,
            width=text_width
        )

        # Applies a white background extension
        caption_img = text.ifthenelse(255, text).gravity("centre", width, text.height + size, extend="white")

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Joins the caption and the image vertically with a white background
            joined = caption_img.join(frame, "vertical", background=[255, 255, 255, 255], expand=True)
            frames.append(joined)

        final = pyvips.Image.arrayjoin(frames, across=1)
        # Sets the new combined height
        final.set_type(
            pyvips.GValue.gint_type,
            "page-height",
            page_height + caption_img.height
        )
        return self._finalize_image(final)

    async def caption(self, buffer_data: bytes, text: str) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_caption, buffer_data, text)

    def _process_caption2(self, buffer_data: bytes, caption_text: str, top: bool) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)

        width, page_height, n_pages = self._get_page_data(img)
        size = width // 13  # Uses a smaller divisor (13) for text size compared to the standard caption
        text_width = width - ((width // 25) * 2)

        markup = f'<span background="white">{caption_text}</span>'
        font_string = f"helvetica {size}"

        # Aligns the text low
        text = pyvips.Image.text(markup, rgba=True, font=font_string, align="low", width=text_width)
        # Embeds the text with white padding
        caption_img = text.ifthenelse(255, text).embed(
            width // 25,
            width // 25, width,
            text.height + size,
            extend="white"
        )

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img

            # Conditionally joins the image either above or below the caption depending on the 'top' boolean
            if top:
                joined = caption_img.join(frame, "vertical", background=[255, 255, 255, 255], expand=True)
            else:
                joined = frame.join(caption_img, "vertical", background=[255, 255, 255, 255], expand=True)
            frames.append(joined)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height + caption_img.height)
        return self._finalize_image(final)

    async def caption2(self, buffer_data: bytes, text: str, top: bool = False) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_caption2, buffer_data, text, top)

    def _polar_map(self, width: int, height: int) -> pyvips.Image:
        xy = pyvips.Image.xyz(width, height)
        xy -= [width / 2.0, height / 2.0]  # Shifts the coordinates
        scale = max(width, height) / width  # Calculates the scale ratio
        xy *= 1.5 / scale  # Applies the scale adjustment
        # Converts the format to complex and calculates polar coordinates
        index_complex = xy.copy(format="complex", bands=1).polar()
        index = index_complex.copy(format="float", bands=2)  # Reverts the format to float with 2 bands
        index *= [1, height / 360.0]  # Scales the Y coordinate to 360 degrees
        return index

    def _rectangular_map(self, width: int, height: int) -> pyvips.Image:
        xy = pyvips.Image.xyz(width, height)
        xy *= [1, 360.0 / height]  # Scales the coordinates based on a 360-degree mapping
        # Converts to complex format and maps to rectangular coordinates
        index_complex = xy.copy(format="complex", bands=1).rect()
        index = index_complex.copy(format="float", bands=2)
        scale = max(width, height) / width / 1.5  # Derives the rectangular scale
        index *= scale
        index += [width / 2.0, height / 2.0]  # Recenters the coordinates
        return index

    def _process_circle(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        rect_index = self._rectangular_map(width, page_height)
        polar_index = self._polar_map(width, page_height)
        # Generates a 90-degree rotated separable gaussian matrix
        gaussmat = pyvips.Image.gaussmat(5, 0.2, separable=True).rot90()

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Maps rectangular coordinates, replicates the image, convolves the gaussian matrix, crops it back, and maps polar coordinates with mirror extension
            result = frame.mapim(rect_index).replicate(
                1, 3
            ).conv(
                gaussmat, precision="integer"
            ).crop(
                0, page_height, width, page_height
            ).mapim(
                polar_index, extend="mirror"
            )
            frames.append(result)

        out = pyvips.Image.arrayjoin(frames, across=1)
        return self._finalize_image(out)

    async def circle(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_circle, buffer_data)

    def _process_colors(self, buffer_data: bytes, color: str, shift: int) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")

        if color == "grayscale":
            out = img.colourspace("b-w")  # Modifies the colorspace to black and white
        elif color == "sepia":
            # Extracts the first 3 bands and recombines them using the predefined sepia matrix
            out = img.extract_band(0, n=3).recomb(self._sepia_matrix)
        elif color == "hueshift":
            if not img.hasalpha(): img = img.bandjoin(255)
            # Converts the image to the LCH colorspace and applies the shift to the hue vector
            out = img.colourspace("lch") + [0, 0, shift, 0]
        else:
            out = img

        return self._finalize_image(out)

    async def colors(self, buffer_data: bytes, color: str, shift: int = 0) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_colors, buffer_data, color, shift)

    def _process_crop(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        frames = []
        final_height = 0
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Determines if width or height is larger to calculate the maximum square size
            size = frame.height if (frame.width / frame.height >= 1) else frame.width
            # Applies smartcrop targeting the center of the image
            result = frame.smartcrop(size, size, interesting="centre")
            final_height = size
            frames.append(result)

        final = pyvips.Image.arrayjoin(frames, across=1)
        # Updates the metadata to the new cropped size
        final.set_type(pyvips.GValue.gint_type, "page-height", final_height)
        return self._finalize_image(final)

    async def crop(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_crop, buffer_data)

    def _process_deepfry(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        # Modifies contrast and brightness mathematically using specific modifiers
        fried = (img * 1.3 - 76.5) * 1.5

        # Writes to a memory buffer as a stripped JPEG with quality set to 1 to introduce artifacts
        jpg_buf = fried.write_to_buffer(".jpg[Q=1,strip=True]")
        final = pyvips.Image.new_from_buffer(jpg_buf, "")

        # Note: If height > 65500, libvips natively errors on jpeg, so we crop individual frames for animations
        if img.height > 65500 and n_pages > 1:
            frames = []
            for i in range(n_pages):
                frame = fried.crop(0, i * page_height, width, page_height)
                frame_buf = frame.write_to_buffer(".jpg[Q=1,strip=True]")
                frames.append(pyvips.Image.new_from_buffer(frame_buf, "").copy_memory())
            final = pyvips.Image.arrayjoin(frames, across=1)

        final.set_type(pyvips.GValue.gint_type, "page-height", page_height)
        return self._finalize_image(final)

    async def deepfry(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_deepfry, buffer_data)

    def _process_fade(self, buffer_data: bytes, alpha: bool) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)  # Appends an alpha channel

        width, page_height, n_pages = self._get_page_data(img)
        multi_page = n_pages > 1

        if n_pages == 1:
            multi_page = False
            n_pages = 30  # Overrides to 30 frames for static images

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if multi_page else img
            mult = i / (n_pages - 1)  # Calculates the fading multiplier across the frame count

            # Isolates the color channels and applies the multiplier logic to the alpha band depending on output format
            faded = frame.extract_band(0, n=frame.bands - 1)
            # Since WebP supports alpha, we multiply the alpha channel itself
            faded = faded.bandjoin(frame.extract_band(frame.bands - 1) * mult)

            frames.append(faded)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height)

        if not multi_page:
            final.set_type(pyvips.GValue.array_int_type, "delay",
                           [50] * 30)  # Enforces a loop and array delay for static conversions

        return self._finalize_image(final)

    async def fade(self, buffer_data: bytes, alpha: bool = False) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_fade, buffer_data, alpha)

    def _process_flag(self, buffer_data: bytes, overlay_name: str) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)  # Forces alpha channel
        width, page_height, n_pages = self._get_page_data(img)

        overlay_path = os.path.join(self.base_path, 'assets', 'images',
                                    overlay_name)  # Uses basePath to fetch the overlay
        overlay = pyvips.Image.new_from_file(overlay_path)

        # Resizes the overlay to match the width and pageHeight of the input
        overlay = overlay.resize(width / overlay.width, vscale=page_height / overlay.height)

        if not overlay.hasalpha():
            overlay = overlay.bandjoin(127)  # Sets transparency to 50% if alpha is missing
        else:
            overlay = overlay * [1, 1, 1, 0.5]  # Multiplies the image array to lower existing alpha to 50%

        replicated = overlay.replicate(1, n_pages)  # Copies the overlay vertically for every frame
        final = img.composite(replicated, "over")  # Composites the flag directly over the base image

        return self._finalize_image(final)

    async def flag(self, buffer_data: bytes, overlay_name: str) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_flag, buffer_data, overlay_name)

    def _process_flip(self, buffer_data: bytes, flop: bool) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        if flop:
            out = img.flip("horizontal")  # Mirrors horizontally if flop argument is true
        elif n_pages > 1:
            frames = []
            for i in range(n_pages):
                # Crops each frame individually and flips it vertically to preserve animation
                frame = img.crop(0, i * page_height, width, page_height).flip("vertical")
                frames.append(frame)
            out = pyvips.Image.arrayjoin(frames, across=1)
            out.set_type(pyvips.GValue.gint_type, "page-height", page_height)
        else:
            out = img.flip("vertical")  # Flips static images vertically directly

        return self._finalize_image(out)

    async def flip(self, buffer_data: bytes, flop: bool = False) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_flip, buffer_data, flop)

    def _process_gamexplain(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)  # Preserves alpha channel
        width, page_height, n_pages = self._get_page_data(img)

        tmpl_path = os.path.join(self.base_path, "assets/images/gamexplain.png")
        tmpl = pyvips.Image.new_from_file(tmpl_path)  # Loads the Gamexplain overlay template

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Resizes the image to 1181x571 and embeds it at position (10, 92) in a 1200x675 canvas with white background
            resized = frame.resize(1181.0 / width, vscale=571.0 / page_height).embed(10, 92, 1200, 675, extend="white")
            # Composites the resized frame behind the gamexplain template
            composited = resized.composite(tmpl, "over")
            frames.append(composited)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", 675)  # Sets metadata to canvas height
        return self._finalize_image(final)

    async def gamexplain(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_gamexplain, buffer_data)

    def _process_globe(self, buffer_data: bytes, snow: bool) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if img.hasalpha(): img = img.flatten()  # Flattens alpha to avoid globe rendering bugs
        width, page_height, n_pages = self._get_page_data(img)

        multi_page = n_pages > 1
        if n_pages == 1:
            multi_page = False
            n_pages = 30  # Forces 30 frames for the 3D rotation

        size = min(width, page_height)  # Uses the smallest side to ensure a perfect circle

        # Loads and resizes the diffuse and specular rendering maps linearly
        specdiff = pyvips.Image.new_from_file(os.path.join(self.base_path, "assets/images/globespecdiff.png")).resize(
            size / 500.0, kernel="linear")
        diffuse = specdiff[1] / 255  # Normalizes diffuse values
        specular = specdiff[0]  # Normalizes specular values

        if snow: diffuse *= 2  # Brightens the diffuse map if snow arg is true

        snow_base = pyvips.Image.new_from_file(os.path.join(self.base_path, "assets/images/snowbase.png")).resize(
            size / 500.0, kernel="linear")
        base_height = snow_base.height  # Extracts the bottom base height
        real_height = int(size * 1.15) if snow else size  # Extends height to fit the globe stand

        distort_img = pyvips.Image.new_from_file(os.path.join(self.base_path, "assets/images/spheremap.png")).resize(
            size / 500.0, kernel="linear")
        distort = ((distort_img / 65535) * size).cast(
            "ushort").copy_memory()  # Calculates distortion mappings by dividing and casting

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if multi_page else img
            # Wraps the image horizontally over time and maps the 3D distortion onto it
            mapped = frame.wrap(x=int(width * i / n_pages), y=0).mapim(distort)
            # Reapplies lighting via diffuse/specular math and adds transparency
            frame_out = (mapped * diffuse + specular).cast("uchar").bandjoin(diffuse > 0.0)

            if snow:
                # Embeds the snowglobe base onto the bottom of the frame
                frame_out = frame_out.embed(0, 0, size, real_height).composite(snow_base, "over", x=0,
                                                                               y=real_height - base_height)
            frames.append(frame_out)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", real_height)
        if not multi_page:
            final.set_type(pyvips.GValue.array_int_type, "delay", [50] * 30)  # Adds frame delay loop

        return self._finalize_image(final)

    async def globe(self, buffer_data: bytes, snow: bool = False) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_globe, buffer_data, snow)

    def _process_homebrew(self, caption: str) -> bytes:
        bg = pyvips.Image.new_from_file(
            os.path.join(self.base_path, "assets/images/hbc.png"))  # Loads the Homebrew channel background

        font_path = os.path.join(self.base_path, "assets/fonts/hbc.ttf")
        # Generates the text using PF Square Sans Pro 96, negative letter spacing, and white color
        markup = f'<span letter_spacing="-5120" color="white">{caption}</span>'
        text = pyvips.Image.text(markup, rgba=True, align="centre", font="PF Square Sans Pro 96", fontfile=font_path)

        # Centers the text mathematically around coordinate (400, 300) with an 8px offset
        out = bg.composite(text, "over", x=400 - (text.width // 2), y=300 - (text.height // 2) - 8)

        return self._finalize_image(out)

    async def homebrew(self, caption: str) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_homebrew, caption)

    def _process_jpeg(self, buffer_data: bytes, quality: int) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        if n_pages > 1:
            frames = []
            for i in range(n_pages):
                frame = img.crop(0, i * page_height, width, page_height)
                # Encodes and decodes the image multiple times as a stripped JPEG frame to force quality compression artifacts
                jpg_buf = frame.write_to_buffer(f".jpg[Q={quality},strip=True]")
                frames.append(pyvips.Image.new_from_buffer(jpg_buf, "").copy_memory())
            final = pyvips.Image.arrayjoin(frames, across=1)
            final.set_type(pyvips.GValue.gint_type, "page-height", page_height)
            return self._finalize_image(final)
        else:
            # Handles static images by directly outputting a stripped JPEG
            return img.write_to_buffer(f".jpg[Q={quality},strip=True]")

    async def jpeg(self, buffer_data: bytes, quality: int = 1) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_jpeg, buffer_data, quality)

    def _gen_meme_text(self, text: str, font: str, fontfile: str, width: int, mask: pyvips.Image,
                       radius: int) -> pyvips.Image:
        markup = f'<span foreground="white">{text}</span>'  # Generates a white foreground span

        kwargs = {"rgba": True, "align": "centre", "font": font, "width": width}
        if fontfile: kwargs["fontfile"] = fontfile  # Connects local font if applicable
        img = pyvips.Image.text(markup, **kwargs)

        # Embeds text to give room for the calculated mask outline
        img = img.embed(radius, radius * 2, img.width + 2 * radius, (img.height + 2 * radius) + (radius * 2))
        new_text = img.convsep(mask)  # Applies a gaussian blur separation matrix to act as drop shadow

        # Casts to UCHAR and multiplies RGB to zero while leaving Alpha intact, enforcing black outline
        outline = new_text.cast("uchar") * [0, 0, 0, 1]
        return outline.composite(img, "over")

    def _process_meme(self, buffer_data: bytes, top_text: str, bottom_text: str, font: str) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)  # Adds alpha for text rendering
        width, page_height, n_pages = self._get_page_data(img)

        size = width / 9.0  # Text scales to one-ninth of the total image width
        radius = int(size / 18.0)  # Mask radius scales to one-eighteenth of size

        # Standardizes font styling to Roboto Condensed or Impact with bold/normal styling respectively
        font_style = f"{'Roboto Condensed' if font == 'roboto' else font} {'normal' if font == 'impact' else 'bold'} {int(size)}"
        font_path = os.path.join(self.base_path, f"assets/fonts/{font}.ttf") if font != "impact" else ""

        mask = pyvips.Image.gaussmat(radius / 2, 0.1,
                                     separable=True) * 8  # Calculates standard gaussian matrix mask multiplied by 8

        # Creates a black canvas with a cleared alpha band to layer the text safely
        combined_text = pyvips.Image.black(width, page_height, bands=3).bandjoin(0).copy(interpretation="srgb")

        if top_text:
            top_img = self._gen_meme_text(top_text, font_style, font_path, width, mask, radius)
            combined_text = combined_text.composite(top_img, "over", x=int((width / 2) - (top_img.width / 2)),
                                                    y=0)  # Aligns top text to y=0

        if bottom_text:
            bottom_img = self._gen_meme_text(bottom_text, font_style, font_path, width, mask, radius)
            combined_text = combined_text.composite(bottom_img, "over", x=int((width / 2) - (bottom_img.width / 2)),
                                                    y=int(
                                                        page_height - bottom_img.height))  # Aligns bottom text near y=pageHeight

        replicated = combined_text.replicate(1, n_pages)  # Copies text across all animation frames
        final = img.composite(replicated, "over")  # Composites text over original image

        return self._finalize_image(final)

    async def meme(self, buffer_data: bytes, top_text: str = "", bottom_text: str = "", font: str = "impact") -> bytes:
        async with self._lock:
            return await asyncio.to_thread(self._process_meme, buffer_data, top_text, bottom_text, font)

    def _process_mirror(self, buffer_data: bytes, vertical: bool, first: bool) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        frames = []
        is_odd = page_height % 2
        new_page_height = page_height

        if vertical:
            if n_pages > 1:
                # Handles the odd/even page height pixel loss
                new_page_height = page_height - (1 if is_odd else 0)
                for i in range(n_pages):
                    x = (i * page_height) + (
                        0 if first else (page_height // 2))  # Calculates the split coordinate
                    cropped = img.crop(0, x, width, page_height // 2)
                    flipped = cropped.flip("vertical")
                    # Joins the flipped half with the original half depending on 'first' flag
                    frame = pyvips.Image.arrayjoin([cropped if first else flipped, flipped if first else cropped],
                                                   across=1)
                    frames.append(frame)
                out = pyvips.Image.arrayjoin(frames, across=1)
                out.set_type(pyvips.GValue.gint_type, "page-height", new_page_height)
            else:
                size = img.height // 2
                y_offset = 0 if first else size
                cropped = img.extract_area(0, y_offset, width, size)
                flipped = cropped.flip("vertical")
                out = pyvips.Image.arrayjoin([cropped if first else flipped, flipped if first else cropped], across=1)
        else:
            size = width // 2
            x_offset = 0 if first else size
            cropped = img.extract_area(x_offset, 0, size, img.height)
            flipped = cropped.flip("horizontal")  # Mirrors horizontally
            out = pyvips.Image.arrayjoin([cropped if first else flipped, flipped if first else cropped])

        return self._finalize_image(out)

    async def mirror(self, buffer_data: bytes, vertical: bool = False, first: bool = False) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_mirror, buffer_data, vertical, first)

    def _process_motivate(self, buffer_data: bytes, top_text: str, bottom_text: str, font: str) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)
        width, page_height, n_pages = self._get_page_data(img)

        size = width // 5  # Font size scales relative to 1/5th of the image width
        text_width = width - ((width // 25) * 2)  # Padding logic for the text width

        font_string = f"{'Roboto Condensed' if font == 'roboto' else font}"  # Fallback for roboto
        font_path = os.path.join(self.base_path, f"assets/fonts/{font}.ttf") if font != "times" else ""

        top_img = None
        if top_text:
            markup = f'<span foreground="white" background="black">{top_text}</span>'
            kwargs = {"rgba": True, "align": "centre", "width": text_width, "font": f"{font_string} {size}"}
            if font_path: kwargs["fontfile"] = font_path
            top_img = pyvips.Image.text(markup, **kwargs)

        bottom_img = None
        if bottom_text:
            markup = f'<span foreground="white" background="black">{bottom_text}</span>'
            kwargs = {"rgba": True, "align": "centre", "width": text_width,
                      "font": f"{font_string} {int(size * 0.4)}"}  # Bottom text is scaled down to 40%
            if font_path: kwargs["fontfile"] = font_path
            bottom_img = pyvips.Image.text(markup, **kwargs)

        frames = []
        final_height = 0
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img

            border_size = max(2, width // 66)
            border_size2 = int(border_size * 0.5)

            # Triple embedding: Black inner border, White middle border, Black outer border
            bordered = frame.embed(border_size, border_size, width + (border_size * 2), page_height + (border_size * 2),
                                   extend="black")
            bordered2 = bordered.embed(border_size2, border_size2, bordered.width + (border_size2 * 2),
                                       bordered.height + (border_size2 * 2), extend="white")

            addition = width // 8
            side_addition = int(page_height * 0.4)
            bordered3 = bordered2.embed(side_addition // 2, addition // 2, bordered2.width + side_addition,
                                        bordered2.height + addition, extend="black")

            final_frame = bordered3
            if top_img:
                # Appends top text above the bordered image using a vertical join
                padded_top = top_img.gravity("north", bordered3.width, top_img.height + (size // 4), extend="black")
                final_frame = final_frame.join(padded_top, "vertical", background=[0, 0, 0, 255], expand=True)
            if bottom_img:
                # Appends bottom text below the bordered image
                padded_bottom = bottom_img.gravity("north", bordered3.width, bottom_img.height + (size // 4),
                                                   extend="black")
                final_frame = final_frame.join(padded_bottom, "vertical", background=[0, 0, 0, 255], expand=True)

            final_height = final_frame.height
            frames.append(final_frame)

        # Joins the frames and extracts only the first 3 bands (RGB) to enforce the final black background natively
        final = pyvips.Image.arrayjoin(frames, across=1).extract_band(0, n=3)
        final.set_type(pyvips.GValue.gint_type, "page-height", final_height)

        return self._finalize_image(final)

    async def motivate(self, buffer_data: bytes, top_text: str = "", bottom_text: str = "",
                       font: str = "times") -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_motivate, buffer_data, top_text,
                                                              bottom_text, font)

    def _process_qr_create(self, text: str) -> bytes:
        qr = qrcode.QRCode(box_size=4, border=1)
        qr.add_data(text)
        qr.make(fit=True)
        # Generates a basic QR code bitmap with 1px margin
        img = qr.make_image(fill_color="black", back_color="white")

        buf = io.BytesIO()
        img.save(buf, format="PNG")

        # Passes the standard Python PIL image back into the pyvips pipeline using Nearest Neighbor resizing to match ZXing C++ output
        vips_img = pyvips.Image.new_from_buffer(buf.getvalue(), "").resize(4, kernel="nearest")
        return vips_img.write_to_buffer(".png")

    async def qr_create(self, text: str) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_qr_create, text)

    def _process_qr_read(self, buffer_data: bytes) -> str:

        # Parses the incoming image strictly to black and white
        img = self._load_image(buffer_data).colourspace("b-w").extract_band(0, n=1)
        png_buf = img.write_to_buffer(".png")

        pil_img = Image.open(io.BytesIO(png_buf))
        results = decode(pil_img)

        if not results:
            return ""  # Returns empty if no QR code is valid/found
        return results[0].data.decode('utf-8')

    async def qr_read(self, buffer_data: bytes) -> str:
        async with self._lock: return await asyncio.to_thread(self._process_qr_read, buffer_data)

    def _process_reddit(self, buffer_data: bytes, caption: str) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)
        width, page_height, n_pages = self._get_page_data(img)

        tmpl = pyvips.Image.new_from_file(
            os.path.join(self.base_path, "assets/images/reddit.png"))  # Loads the reddit overlay
        font_path = os.path.join(self.base_path, "assets/fonts/reddit.ttf")

        markup = f'<span foreground="white">Posted in r/{caption}</span>'

        # Calculates the font baseline offset using a dummy 'A' character
        baseline_calc = pyvips.Image.text("A", font="Roboto 62", fontfile=font_path)
        baseline = baseline_calc.height + (
            baseline_calc.get("yoffset") if "yoffset" in baseline_calc.get_fields() else 0)

        text_img = pyvips.Image.text(markup, rgba=True, font="Roboto 62", fontfile=font_path)
        y_offset = text_img.get("yoffset") if "yoffset" in text_img.get_fields() else 0

        # Composites the text directly onto the watermark background using the calculated baseline coordinate math
        composited = tmpl.composite(text_img, "over", x=64, y=int((tmpl.height - baseline) - 64 + y_offset))
        watermark = composited.resize(
            width / composited.width)  # Scales watermark to fit the primary image width

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Attaches the watermark permanently to the bottom of the frame
            final_frame = frame.join(watermark, "vertical", expand=True)
            frames.append(final_frame)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height + watermark.height)
        return self._finalize_image(final)

    async def reddit(self, buffer_data: bytes, caption: str) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_reddit, buffer_data, caption)

    def _process_reverse(self, buffer_data: bytes, soos: bool) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        if n_pages < 2:
            return buffer_data  # Ignores static images completely and returns the raw input buffer

        delays = img.get("delay") if "delay" in img.get_fields() else [50] * n_pages
        frames = []
        delays_out = []

        if soos:
            # Plays the animation forward
            for i in range(n_pages):
                frames.append(img.crop(0, i * page_height, width, page_height))
                delays_out.append(delays[i])
            # Plays the animation backwards immediately after
            for i in range(n_pages - 2, 0, -1):
                frames.append(img.crop(0, i * page_height, width, page_height))
                delays_out.append(delays[i])
        else:
            # Simply reverses the animation completely
            for i in range(n_pages - 1, -1, -1):
                frames.append(img.crop(0, i * page_height, width, page_height))
                delays_out.append(delays[i])

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height)
        final.set_type(pyvips.GValue.array_int_type, "delay", delays_out)  # Re-applies the reordered delays

        return self._finalize_image(final)

    async def reverse(self, buffer_data: bytes, soos: bool = False) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_reverse, buffer_data, soos)

    def _process_scott(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)
        width, page_height, n_pages = self._get_page_data(img)

        bg = pyvips.Image.new_from_file(os.path.join(self.base_path, "assets/images/scott.png"))
        distort = pyvips.Image.new_from_file(os.path.join(self.base_path, "assets/images/scottmap.png"))

        # Creates a complex map by separating the distortion channels, dividing by 255, and scaling them to match the TV screen dimensions (414x233)
        distort_img = ((distort[1] / 255) * 414).bandjoin((distort[0] / 255) * 233)

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Resizes image to fit the raw TV map
            resized = frame.resize(415 / width, vscale=234 / page_height)
            # Maps the 3D TV distortion map over the image, extracts RGB, and re-attaches the alpha channel from the distortion template
            mapped = resized.mapim(distort_img).extract_band(0, n=3).bandjoin(distort[2])
            # Embeds the fully distorted image into the overall 864x481 canvas coordinate
            offset = mapped.embed(127, 181, 864, 481)
            # Layers the Scott overlay on top
            composited = bg.composite(offset, "over")
            frames.append(composited)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", 481)
        return self._finalize_image(final)

    async def scott(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_scott, buffer_data)

    def _process_slide(self, buffer_data: bytes, vertical: bool) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        width, page_height, n_pages = self._get_page_data(img)

        multi_page = n_pages > 1
        if n_pages == 1:
            multi_page = False
            n_pages = 15  # Upconverts static inputs to 15 frames

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if multi_page else img
            # Calculates the frame's wrap offset dynamically based on the frame iteration, direction, and reverse multiplier
            x_val = 0 if vertical else int(width * i / n_pages)
            y_val = int(page_height * i / n_pages) if vertical else 0

            # Wraps the image infinitely around its own edges
            wrapped = frame.wrap(x=x_val, y=y_val)
            frames.append(wrapped)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height)
        if not multi_page:
            final.set_type(pyvips.GValue.array_int_type, "delay", [50] * 15)

        return self._finalize_image(final)

    async def slide(self, buffer_data: bytes, vertical: bool = False) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_slide, buffer_data, vertical)

    def _process_sonic(self, caption: str) -> bytes:
        bg = pyvips.Image.new_from_file(
            os.path.join(self.base_path, "assets/images/sonic.jpg"))  # Loads the Sonic template

        markup = f'<span foreground="white">{caption}</span>'
        # Renders Verdana text clamped to a width of 542 and height of 390
        text_img = pyvips.Image.text(markup, rgba=True, align="centre", font="Verdana", width=542, height=390)

        # Centers the generated text via gravity before laying it over the speech bubble at coordinate (391, 84)
        text_img = text_img.gravity("centre", 542, 390)
        out = bg.composite(text_img, "over", x=391, y=84)

        return self._finalize_image(out)

    async def sonic(self, caption: str) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_sonic, caption)

    def _process_speed(self, buffer_data: bytes, slow: bool, speed_val: float) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        # Retrieves existing frame delays or defaults to 50ms per frame
        delays = img.get("delay") if "delay" in img.get_fields() else [50] * n_pages
        frames = []
        new_delays = []

        if slow:
            for i in range(n_pages):
                frames.append(img.crop(0, i * page_height, width, page_height))
                new_delays.append(int(delays[i] * speed_val))  # Multiplies delay to slow down
        else:
            # If speeding up pushes delays too low (<=10ms), it drops frames instead of changing delays
            drop_frames = any((d / speed_val) <= 10 for d in delays)

            if drop_frames:
                i = 0.0
                while int(i) < n_pages:
                    idx = int(i)
                    frames.append(img.crop(0, idx * page_height, width, page_height))
                    new_delays.append(delays[idx])
                    i += speed_val  # Skips frames via float iteration
            else:
                for i in range(n_pages):
                    frames.append(img.crop(0, i * page_height, width, page_height))
                    new_delays.append(int(delays[i] / speed_val))

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height)
        final.set_type(pyvips.GValue.array_int_type, "delay", new_delays)

        return self._finalize_image(final)

    async def speed(self, buffer_data: bytes, slow: bool = False, speed_val: float = 2.0) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_speed, buffer_data, slow, speed_val)

    def _process_spin(self, buffer_data: bytes, angle: int) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)
        width, page_height, n_pages = self._get_page_data(img)

        multi_page = n_pages > 1
        if n_pages == 1:
            multi_page = False
            if angle == 0: n_pages = 30  # Forces 30 frames for a full rotation animation

        frames = []
        out_page_height = page_height
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if multi_page else img
            # Uses the static angle if provided, otherwise calculates the frame's progression through 360 degrees
            rotation = angle if angle > 0 else 360.0 * i / n_pages
            rotated = frame.rotate(rotation)

            # Embeds rotating frames into a static bounding box to prevent the canvas from wobbling
            embedded = rotated if angle > 0 else rotated.embed((width // 2) - (rotated.width // 2),
                                                               (page_height // 2) - (rotated.height // 2), width,
                                                               page_height)
            out_page_height = embedded.height
            frames.append(embedded)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", out_page_height)
        if not multi_page and angle == 0:
            final.set_type(pyvips.GValue.array_int_type, "delay", [50] * 30)

        return self._finalize_image(final)

    async def spin(self, buffer_data: bytes, angle: int = 0) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_spin, buffer_data, angle)

    def _process_squish(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        multi_page = n_pages > 1
        if n_pages == 1:
            multi_page = False
            n_pages = 30  # Generates 30 frames for static inputs

        mult = 6.28 / n_pages  # Pi * 2 multiplier for a full sine/cosine cycle

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if multi_page else img
            # Calculates alternating compression using sine for width and cosine for height
            new_width = (math.sin(i * mult) / 4) + 0.75
            new_height = (math.cos(i * mult) / 4) + 0.75
            # Resizes based on the wave and snaps the gravity to center
            resized = frame.resize(new_width, vscale=new_height).gravity("centre", width, page_height)
            frames.append(resized)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height)
        if not multi_page:
            final.set_type(pyvips.GValue.array_int_type, "delay", [50] * 30)

        return self._finalize_image(final)

    async def squish(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_squish, buffer_data)

    def _process_swirl(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        new_width = width * 3
        new_height = page_height * 3
        div_x, div_y = new_width / 2.0, new_height / 2.0

        # Builds a coordinate map and converts it to polar complex coordinates
        index = pyvips.Image.xyz(new_width, new_height)
        center = index - [div_x, div_y]
        polar = center.copy(format="complex", bands=1).polar().copy(format="float", bands=2)

        size = min(width, page_height) / 2.0

        # Calculates swirl angle distortion exponentially based on distance from the center
        test = (1 - polar.extract_band(0) / size)
        degrees = test.cast("float") ** 2
        angle = polar.extract_band(1) + degrees * 180

        # Converts back to rectangular coordinates for the final mapping
        distortion = polar.extract_band(0).bandjoin(angle).copy(format="complex", bands=1).rect().copy(format="float",
                                                                                                       bands=2) + [
                         div_x, div_y]

        frames = []
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Centers the frame on the 3x canvas, maps the swirl distortion using bicubic interpolation, and crops back to original bounds
            distorted = frame.gravity("centre", new_width, new_height, extend="copy").mapim(distortion,
                                                                                            interpolate=pyvips.Interpolate.new(
                                                                                                "bicubic"))
            final_frame = distorted.crop(width, page_height, width, page_height)
            frames.append(final_frame)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", page_height)
        return self._finalize_image(final)

    async def swirl(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_swirl, buffer_data)

    def _process_tile(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data)
        width, page_height, n_pages = self._get_page_data(img)

        frames = []
        final_height = 0
        for i in range(n_pages):
            frame = img.crop(0, i * page_height, width, page_height) if n_pages > 1 else img
            # Replicates the image in a 5x5 grid
            replicated = frame.replicate(5, 5)

            # Calculates downward scaling to ensure the max dimension does not exceed 800px
            scale = 800.0 / replicated.height
            if scale > 1: scale = 800.0 / replicated.width
            if scale < 1: replicated = replicated.resize(scale)

            final_height = replicated.height
            frames.append(replicated)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", final_height)
        return self._finalize_image(final)

    async def tile(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_tile, buffer_data)

    def _process_wall(self, buffer_data: bytes) -> bytes:
        img = self._load_image(buffer_data)
        if not img.hasalpha(): img = img.bandjoin(255)
        width, page_height, n_pages = self._get_page_data(img)

        max_size = max(width, page_height)
        # Calculates a vertical scaling factor to ensure uniform height before perspective distortion
        vscale = int(page_height * (128.0 / max_size)) / float(page_height)

        # Downscales to a 128x128 box to reduce resolution, then immediately blows back up to 512x512
        tile = img.resize(128.0 / max_size, vscale=vscale).resize(512.0 / 128.0).copy_memory()

        new_height = tile.height
        width = tile.width
        page_height = new_height // n_pages if n_pages > 1 else new_height

        # Perspective Transform matrix (computed from OpenCV for ImageMagick point sets)
        T = [1.32996610e+00, -9.06795066e-02, -7.19995282e+01, -2.75152214e-01, 1.26875743e+00, -3.76041359e+01,
             -7.70327830e-04, -7.08433645e-04]

        i_img = pyvips.Image.xyz(512, 512)
        # Calculates the transform mathematics using the matrix
        xi = (i_img[0] * T[0] + i_img[1] * T[1] + T[2]) / (i_img[0] * T[6] + i_img[1] * T[7] + 1)
        yi = (i_img[0] * T[3] + i_img[1] * T[4] + T[5]) / (i_img[0] * T[6] + i_img[1] * T[7] + 1)

        xi = xi % width
        yi = yi % page_height
        index = xi.bandjoin(yi).copy_memory()  # Binds the transform coordinates into a mapim index

        frames = []
        for i in range(n_pages):
            frame = tile.crop(0, i * page_height, width, page_height) if n_pages > 1 else tile
            # Maps the 3D wall distortion using the computed index matrix
            mapped = frame.mapim(index, extend="repeat")
            frames.append(mapped)

        final = pyvips.Image.arrayjoin(frames, across=1)
        final.set_type(pyvips.GValue.gint_type, "page-height", 512)
        return self._finalize_image(final)

    async def wall(self, buffer_data: bytes) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_wall, buffer_data)

    def _process_whisper(self, buffer_data: bytes, caption: str) -> bytes:
        img = self._load_image(buffer_data).colourspace("srgb")
        if not img.hasalpha(): img = img.bandjoin(255)
        width, page_height, n_pages = self._get_page_data(img)

        size = width // 6
        rad = size / 24.0  # Defines the drop-shadow radius relative to the text size

        font_string = f"Upright {size}"
        font_path = os.path.join(self.base_path, "assets/fonts/whisper.otf")

        # Gaussian blur mask multiplied by 8 for severe darkening
        mask = pyvips.Image.gaussmat(rad / 2, 0.1, separable=True) * 8

        markup = f'<span foreground="white">{caption}</span>'
        text_in = pyvips.Image.text(markup, rgba=True, align="centre", font=font_string, fontfile=font_path,
                                    width=width)
        # Embeds padding around the text to allow room for the shadow expansion
        text_in = text_in.embed(int(rad), int(rad), text_in.width + int(2 * rad), text_in.height + int(2 * rad))

        new_text = text_in.convsep(mask)  # Applies separation matrix
        outline = new_text.cast("uchar") * [0, 0, 0, 1]  # Isolates black outline

        composited = outline.composite(text_in, "over")
        text_img = composited.embed((width // 2) - (composited.width // 2),
                                    (page_height // 2) - (composited.height // 2), width,
                                    page_height)  # Centers the text onto canvas

        replicated = text_img.copy(interpretation="srgb").replicate(1, n_pages)
        final = img.composite(replicated, "over")  # Overlays the text on every frame

        return self._finalize_image(final)

    async def whisper(self, buffer_data: bytes, caption: str) -> bytes:
        async with self._lock: return await asyncio.to_thread(self._process_whisper, buffer_data, caption)
