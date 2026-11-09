from types import SimpleNamespace

from agent.ocr.adapter import OcrAdapter


def test_pinned_table_pipeline_uses_page_ocr_without_uninitialized_cell_recognizer():
    class Pipeline:
        def predict(self, image, **options):
            assert options['use_table_orientation_classify'] is False
            assert options['use_ocr_results_with_table_cells'] is False
            yield SimpleNamespace(json={'res': {'width': 100, 'height': 200,
                'doc_preprocessor_res': {'angle': 270}, 'parsing_res_list': [
                    {'block_label': 'text', 'block_content': 'Table 4-1', 'block_bbox': [10, 20, 30, 40]}]}})
    adapter = OcrAdapter.__new__(OcrAdapter)
    adapter.pipeline = Pipeline()
    adapter.device = 'cpu'
    result = adapter.recognize_page(SimpleNamespace(shape=(100, 200, 3)), 31)
    assert result['regions'][0]['bbox'] == [0.1, 0.7, 0.2, 0.9]
    assert (result['width'], result['height']) == (200, 100)
