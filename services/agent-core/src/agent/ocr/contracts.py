"""Small, JSON-only document recognition results with bounded geometry."""
from __future__ import annotations

from html.parser import HTMLParser
import math

from agent.storage import CoreDataError


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.row = -1
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row += 1
        if tag in {'td', 'th'}:
            values = dict(attrs)
            self.active = {'row': max(0, self.row), 'rowspan': int(values.get('rowspan', 1)),
                'colspan': int(values.get('colspan', 1)), 'header': tag == 'th', 'text': ''}
        if tag == 'br' and self.active is not None:
            self.active['text'] += '\n'

    def handle_data(self, data):
        if self.active is not None:
            self.active['text'] += data

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.active is not None:
            self.active['text'] = self.active['text'].strip()
            self.cells.append(self.active)
            self.active = None


def table_rows(html):
    if not isinstance(html, str) or len(html) > 2_000_000:
        raise CoreDataError('ocr_result_invalid', 'Table result exceeds the limit')
    parser = _TableParser()
    try:
        parser.feed(html)
        grid = {}
        for cell in parser.cells:
            if not 1 <= cell['rowspan'] <= 1000 or not 1 <= cell['colspan'] <= 256 or cell['row'] > 1000:
                raise ValueError('Table span exceeds the limit')
            column = 0
            while (cell['row'], column) in grid:
                column += 1
            if column + cell['colspan'] > 256:
                raise ValueError('Table column exceeds the limit')
            cell['column'] = column
            for row in range(cell['row'], cell['row'] + cell['rowspan']):
                for col in range(column, column + cell['colspan']):
                    if (row, col) in grid:
                        raise ValueError('Overlapping table cells')
                    grid[row, col] = cell['text']
        height = max((row for row, _ in grid), default=-1) + 1
        width = max((column for _, column in grid), default=-1) + 1
        rows = [[grid.get((row, column), '') for column in range(width)] for row in range(height)]
        return {'cells': parser.cells, 'rows': rows}
    except (ValueError, TypeError) as error:
        raise CoreDataError('ocr_result_invalid', 'Invalid table structure') from error


def normalize_page(raw, page, width, height):
    if type(page) is not int or not 1 <= page <= 10_000 or width <= 0 or height <= 0:
        raise CoreDataError('ocr_result_invalid', 'Invalid page dimensions')
    blocks = raw.get('parsing_res_list', [])
    if not isinstance(blocks, list) or len(blocks) > 10_000:
        raise CoreDataError('ocr_result_invalid', 'Invalid region count')
    regions = []
    for index, block in enumerate(blocks):
        text = block.get('block_content', '')
        bbox = block.get('block_bbox')
        if not isinstance(text, str) or len(text) > 2_000_000 or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise CoreDataError('ocr_result_invalid', 'Invalid OCR region')
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in bbox):
            raise CoreDataError('ocr_result_invalid', 'Invalid OCR coordinates')
        x0, y0, x1, y1 = bbox
        if x1 < x0 or y1 < y0 or x0 < -2 or y0 < -2 or x1 > width + 2 or y1 > height + 2:
            raise CoreDataError('ocr_result_invalid', 'OCR region is outside the page')
        label = str(block.get('block_label', 'text'))
        region = {'id': f'p{page}-r{index}', 'kind': label, 'text': text.strip(),
            'bbox': [max(0, x0 / width), max(0, y0 / height), min(1, x1 / width), min(1, y1 / height)],
            'review_status': 'machine', 'confidence': None, 'extraction_method': 'ocr'}
        if label == 'table' and '<table' in text.lower():
            region['table'] = table_rows(text)
            region['text'] = '\n'.join(' | '.join(row) for row in region['table']['rows'])
            if any(len(row) > 12 for row in region['table']['rows']):
                region['review_status'] = 'needs_review'
                region['quality_warning'] = 'Dense table. Verify row, column, unit and footnote relationships.'
        if region['text']:
            regions.append(region)
    return {'page': page, 'width': width, 'height': height, 'regions': regions, 'status': 'completed'}


def restore_bbox(bbox, angle):
    x0, y0, x1, y1 = bbox
    if angle == 90:
        return [1 - y1, x0, 1 - y0, x1]
    if angle == 180:
        return [1 - x1, 1 - y1, 1 - x0, 1 - y0]
    if angle == 270:
        return [y0, 1 - x1, y1, 1 - x0]
    return list(bbox)
