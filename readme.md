# ESMpp (bot)

### VERY EARLY DEVELOPMENT, BARELY WORKS, USE AT YOUR OWN PERIL

XMPP/Python port/remake of [esmbot](https://github.com/esmBot/esmBot/). So far only image manipulation has been ported using
[pyvips](https://pypi.org/project/pyvips/) so that we don't need to make bindings ourselves.

All image processing is offloaded to background threads to keep the XMPP event loop responsive. Animated formats
(WebP/GIF) are fully supported and automatically extrapolated across all manipulation commands.

To use, either directly include a link (i.e. `,read-qr https://dl.4d2.sh/tTiHCcnSx6oX.webp`), or it will use the last OOB
embedded media it has seen in this chat.

**Filters & Distortion**

* `blur`, `sharpen`, `invert`
* `colors [color] [shift]`
* `deepfry`, `jpeg [optional: quality]`

**Motion & Transformations**

* `bounce`, `shake`, `square` (crop), `spin`, `squish`
* `flip`, `flop`, `swirl`, `tile`, `wall`,
* `mirror`, `mirror2`, `mirror3`, `mirror4`
* `slide`, `slide2`
* `reverse`, `soos`, `fade`
* `speed [factor]`
* `slow [factor]`

**Generators & Overlays**

* `caption [text]`, `caption2 [text]`, `whisper [text]`
* `overlay [asset_name]`
* `gamexplain`, `globe`, `snowglobe`, `scott`
* `reddit [subreddit_name]`
* `homebrew [text]`, `sonic [text]`

**Utilities**

* `create-qr [text]`
* `read-qr` (Triggered by replying to an existing image URL)

## Requirements

* Python 3.something
* System Dependency: `pillow` (e.g., `dnf install pillow` on Fedora)
* Python Packages: `pip install -r requirements.txt`
