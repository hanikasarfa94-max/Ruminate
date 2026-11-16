"""Create a self-authored image-only PDF for host vision smoke tests.

The token and chart below are intentionally rendered only as pixels. The PDF
has no searchable text layer and the smoke-test prompt must not reveal them.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    image = Image.new("RGB", (1000, 1300), "white")
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=44)
    text = ImageFont.load_default(size=34)
    draw.text((60, 65), "Ruminate visual reading test", fill="black", font=title)
    draw.text((60, 160), "Verification token:", fill="black", font=text)
    draw.text((60, 215), "CERULEAN-4827", fill="black", font=title)
    draw.text((60, 320), "Synthetic chart: completed tasks", fill="black", font=text)
    draw.line((160, 920, 860, 920), fill="black", width=4)
    draw.line((160, 430, 160, 920), fill="black", width=4)
    draw.rectangle((260, 680, 420, 918), fill="#2974b6")
    draw.rectangle((590, 530, 750, 918), fill="#dd792e")
    draw.text((305, 625), "24", fill="black", font=title)
    draw.text((635, 475), "39", fill="black", font=title)
    draw.text((260, 955), "Orchard", fill="black", font=text)
    draw.text((600, 955), "Harbor", fill="black", font=text)
    draw.text((60, 1090), "Fictional values for software testing.", fill="black", font=text)
    draw.text((60, 1150), "This is not research evidence.", fill="black", font=text)
    image.save(
        Path(__file__).with_name("vision_scan.pdf"),
        format="PDF",
        resolution=144,
        title="Synthetic vision page",
        author="Ruminate Test Fixture",
        creationDate="D:20261111000000Z",
        modDate="D:20261111000000Z",
    )


if __name__ == "__main__":
    main()
