"""Exact, declaration-based Forge KPI identity checks shared by intake and replay."""


def validate_model_identity(document, recipe, model):
    checkpoint = recipe['serving']['model']
    for test in document['tests']:
        labels = test.get('labels', {})
        if 'served_model' in labels:
            if labels['served_model'] != checkpoint:
                raise ValueError('KPI served model attribution disagrees with recipe')
            base = model.get('huggingface_id')
            if not isinstance(base, str) or not base or labels.get('model_name') != base:
                raise ValueError('KPI base model attribution disagrees with model declaration')
        elif ('model_name' in labels or 'quantization' in labels) and labels.get('model_name') != checkpoint:
            # Historical labels prove a checkpoint only when they match it exactly.
            # Unlabeled legacy tests retain the existing run-tag attribution policy.
            raise ValueError('KPI model attribution requires the exact served checkpoint')
        if 'quantization' not in labels:
            continue
        quantization = labels['quantization']
        if not isinstance(quantization, str) or not quantization or not any(
                item.get('huggingface_id') == checkpoint and item.get('name') == quantization
                for item in model.get('quantizations', [])):
            raise ValueError('KPI quantization attribution disagrees with model declaration')
