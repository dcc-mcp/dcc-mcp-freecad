"""Test-only native PNG witness; this is not a public MCP capture capability."""

import hashlib
import importlib.util
import sys
from pathlib import Path

path = Path(__file__).with_name("presentation_host.py")
spec = importlib.util.spec_from_file_location("mcp_native_fixture", str(path))
fixture = importlib.util.module_from_spec(spec)
arguments = sys.argv
try:
    sys.argv = [str(path)]
    spec.loader.exec_module(fixture)
finally:
    sys.argv = arguments


pixel_path = Path(__file__).resolve().parents[1] / "e2e_support/pixels.py"
pixel_spec = importlib.util.spec_from_file_location("mcp_pixel_witness", str(pixel_path))
pixel_witness = importlib.util.module_from_spec(pixel_spec)
pixel_spec.loader.exec_module(pixel_witness)


def _capture(params):
    before = fixture._inspect(params)
    App, Gui = fixture._gui()
    from PySide import QtGui

    doc = App.openDocument(params["document_path"])
    try:
        view = Gui.getDocument(doc.Name).activeView()
        raw_path, output = params["raw_path"], params["output_path"]
        view.saveImage(raw_path, 512, 512, "White", "", 4)
        image = QtGui.QImage(raw_path).convertToFormat(QtGui.QImage.Format_RGBA8888)
        assert not image.isNull() and image.width() == image.height() == 512

        def pixels(image):
            bits = image.constBits()
            size = image.bytesPerLine() * image.height()
            return bits.asstring(size) if hasattr(bits, "asstring") else bytes(bits[:size])

        data = pixels(image)
        geometry_pixels = pixel_witness.colored_geometry(data, 512, 512)
        clean = QtGui.QImage(data, 512, 512, image.bytesPerLine(), image.format()).copy()
        assert clean.save(output, "PNG"), "Native Qt PNG save failed"
        reopened = QtGui.QImage(output).convertToFormat(QtGui.QImage.Format_RGBA8888)
        assert not reopened.textKeys(), "Public PNG contains textual metadata"
        assert pixels(reopened) == data, "Native metadata removal changed pixels"
    finally:
        App.closeDocument(doc.Name)
    after = fixture._inspect(params)
    assert before == after, "Native capture changed recorded document state"
    return {
        "width": 512,
        "height": 512,
        "rgba_sha256": hashlib.sha256(data).hexdigest(),
        "text_keys": [],
        "nonuniform": True,
        "colored_geometry": geometry_pixels,
        "requested_samples": 4,
        "effective_samples": None,
        "snapshot": after,
        "capture_role": "test-only native fixture; no MCP capture endpoint",
    }


if __name__ == "__main__" and "--pass" in sys.argv:
    driver = fixture._driver()
    driver._METHODS["fixture.capture"] = _capture
    driver.main()
