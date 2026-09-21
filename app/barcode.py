from __future__ import annotations

import io

import barcode
from barcode.writer import ImageWriter


def generate_barcode_png(tracking_code: str) -> bytes:
    writer = ImageWriter()  # ty: ignore[call-non-callable]
    code = barcode.Code128(tracking_code, writer=writer)
    buffer = io.BytesIO()
    code.write(
        buffer,
        options={
            "write_text": False,
            "quiet_zone": 6.5,
            "module_width": 0.5,
            "module_height": 20.0,
            "dpi": 300,
        },
    )
    return buffer.getvalue()
