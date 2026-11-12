import pytest

from agent.ocr.contracts import normalize_page, restore_bbox, table_rows
from agent.storage import CoreDataError


def test_table_cells_preserve_headers_negative_values_and_spans():
    html = '<table><tr><th rowspan="2">Test</th><th colspan="2">Category A</th></tr><tr><th>1</th><th>2</th></tr><tr><td>Low temp (C)</td><td>-55</td><td>-20*</td></tr></table>'
    result = table_rows(html)
    assert result['rows'][1] == ['Test', '1', '2']
    assert result['rows'][2] == ['Low temp (C)', '-55', '-20*']
    assert result['cells'][0]['rowspan'] == 2
    assert result['cells'][1]['colspan'] == 2


def test_page_regions_keep_original_location_and_table_text():
    raw = {'parsing_res_list': [
        {'block_label': 'text', 'block_content': '4.3 Equipment categories', 'block_bbox': [0, 0, 100, 20]},
        {'block_label': 'table', 'block_content': '<table><tr><th>A</th><th>B</th></tr><tr><td>-55</td><td>70</td></tr></table>', 'block_bbox': [0, 20, 100, 200]},
    ]}
    result = normalize_page(raw, 31, 100, 200)
    assert result['page'] == 31
    assert result['regions'][1]['bbox'] == [0, .1, 1, 1]
    assert result['regions'][1]['table']['rows'] == [['A', 'B'], ['-55', '70']]
    assert '-55' in result['regions'][1]['text']
    assert result['regions'][1]['review_status'] == 'machine'


def test_invalid_coordinates_are_rejected():
    with pytest.raises(CoreDataError):
        normalize_page({'parsing_res_list': [{'block_label': 'text', 'block_content': 'Bad',
            'block_bbox': [0, 0, float('nan'), 10]}]}, 1, 100, 100)


def test_rotated_ocr_box_maps_back_to_original_page():
    assert restore_bbox([.1, .2, .8, .7], 270) == pytest.approx([.2, .2, .7, .9])
    assert restore_bbox([.1, .2, .8, .7], 90) == pytest.approx([.3, .1, .8, .8])


def test_dense_table_is_searchable_with_visible_quality_flag():
    html = '<table><tr>' + ''.join(f'<td>{i}</td>' for i in range(20)) + '</tr></table>'
    page = normalize_page({'parsing_res_list': [{'block_label': 'table', 'block_content': html,
        'block_bbox': [0, 0, 100, 100]}]}, 31, 100, 100)
    assert page['regions'][0]['review_status'] == 'needs_review'
    assert page['regions'][0]['text']
