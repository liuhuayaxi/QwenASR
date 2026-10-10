"""Label inferred image/chart content without guessing corrected scientific data."""
import json
import re
import zipfile
from pathlib import Path

MARKER = '[MODEL_IMAGE_ANALYSIS_UNVERIFIED]'
WARNING = (MARKER + ' 以下图像/图表说明和数值为模型推断，未经真值核验，不能视为原文测量数据；'
           '请以原图为准。 Image/chart analysis and numeric estimates are model-generated, '
           'unverified, and are not source measurements. Verify against the original image.')


def annotate_export(folder):
    """Preserve exact original export, then label the actual downstream artifacts."""
    folder = Path(folder)
    archive = folder/'raw-mineru.zip'
    if archive.exists():
        return  # Already annotated; preserve the first raw export byte-for-byte.
    paths = [p for p in folder.rglob('*') if p.is_file()]
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as raw:
        for path in paths:
            raw.write(path, str(path.relative_to(folder)))
    marked = 0

    def visit(value, content_list=False):
        nonlocal marked
        if isinstance(value, list):
            for item in value:
                visit(item, content_list)
        elif isinstance(value, dict):
            kind = value.get('type')
            if (kind in {'chart_body', 'image_body'} or
                    (content_list and kind in {'chart', 'image'})) and isinstance(value.get('content'), str) and value['content'].strip():
                if MARKER not in value['content']:
                    value['content'] = WARNING + '\n\n' + value['content']
                    value['analysis_source'] = 'model_generated_unverified'
                    marked += 1
            for child in list(value.values()):
                if isinstance(child, (dict, list)):
                    visit(child, content_list)

    for path in paths:
        if path.suffix == '.json':
            value = json.loads(path.read_text(encoding='utf-8'))
            visit(value, path.name == 'document_content_list.json')
            if isinstance(value, dict):
                value.setdefault('extensions', {})['qwenasr_quality'] = {
                    'scientific_symbols': 'unverified', 'image_analysis': 'model_generated_unverified',
                    'original_images_authoritative': True, 'raw_export': 'raw-mineru.zip'}
            path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        elif path.suffix == '.md':
            text = path.read_text(encoding='utf-8')
            # MinerU places inferred chart tables inside details sections.
            text = re.sub(r'(<details>\s*<summary>.*?</summary>)',
                          lambda match: match[0] + '\n\n> ' + WARNING,
                          text, flags=re.DOTALL)
            path.write_text('> OCR/公式和科学符号未逐项核验；有歧义时请核对原始页面。\n\n' + text,
                            encoding='utf-8')
    (folder/'quality-provenance.json').write_text(json.dumps({
        'state': 'unverified', 'annotation': WARNING, 'annotated_blocks': marked,
        'raw_export': 'raw-mineru.zip', 'raw_export_is_unverified_model_output': True}, ensure_ascii=False), encoding='utf-8')
