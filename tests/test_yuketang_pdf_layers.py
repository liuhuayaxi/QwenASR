from tests import isolation  # noqa: F401
import io
from pathlib import Path
import tempfile
import unittest

from qwen_asr_app.native_pdf import pdf_operation


class YuketangPdfLayerTests(unittest.TestCase):
    def fixture(self, folder, broken=False):
        import pymupdf as fitz
        from PIL import Image, ImageDraw
        bitmap=Image.new('RGB',(800,600),'white')
        draw=ImageDraw.Draw(bitmap)
        # Teacher's green diagram must survive; only the browser circle changes.
        draw.rectangle((400,100,600,150), fill=(40,207,110))
        for y in (200,300):draw.ellipse((100,y,140,y+40),fill=(153,153,153))
        buffer=io.BytesIO();bitmap.save(buffer,'PNG')
        with fitz.open() as doc:
            page=doc.new_page(width=840,height=640)
            page.insert_text((20,620),'《test》',fontname='china-s')
            page.insert_text((400,620),'1/1页',fontname='china-s')
            if not broken:page.insert_image(fitz.Rect(20,20,820,620),stream=buffer.getvalue())
            else:
                icon=Image.new('RGB',(28,32),'gray');b=io.BytesIO();icon.save(b,'PNG')
                page.insert_image(fitz.Rect(20,20,48,52),stream=b.getvalue())
            for label,y,color in [('A',220,(.6,.6,.6)),('B',320,(.1569,.8118,.4314))]:
                page.draw_circle((140,y+20),20,color=None,fill=color)
                page.insert_text((134,y+25),label,fontsize=16,color=(1,1,1))
            path=folder/'source.pdf';doc.save(path)
        return path

    def test_exact_slide_bitmap_removes_personal_green_and_preserves_teaching_green(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=self.fixture(root)
            info=pdf_operation('yuketang_layers',path=str(source))[0]
            self.assertTrue(info['student_selection'])
            output=root/'clean.png'
            pdf_operation('render',path=str(source),neutralize_yuketang=True,
                          pages=[{'page':1,'output':str(output),'scale':1}])
            with Image.open(output) as image:
                self.assertLess(max(image.getpixel((140,340)))-min(image.getpixel((140,340))),3)
                self.assertEqual(image.getpixel((500,140)),(40,207,110))

    def test_missing_base_is_rejected_instead_of_exporting_a_broken_figure(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=self.fixture(root,broken=True)
            self.assertTrue(pdf_operation('yuketang_layers',path=str(source))[0]['missing_slide'])
            with self.assertRaisesRegex(ValueError,'缺少题目底图'):
                pdf_operation('render',path=str(source),neutralize_yuketang=True,
                              pages=[{'page':1,'output':str(root/'bad.png'),'scale':1}])
            self.assertFalse((root/'bad.png').exists())


if __name__ == '__main__':
    unittest.main()
