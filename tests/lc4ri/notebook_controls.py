"""Perform LC4RI cell operations using the visible Classic Notebook controls."""
from types import SimpleNamespace


def source(cell):
    value = cell['source']
    return ''.join(value) if isinstance(value, list) else value


def matching_cells(notebook, reference, fragment, *, count=1):
    indices = [i for i, cell in enumerate(reference['cells'])
               if cell['cell_type'] == 'code' and fragment in source(cell)]
    if len(indices) != count:
        raise ValueError(f'{notebook}: expected {count} code cell(s) containing {fragment!r}; '
                         f'found {len(indices)} at indices {indices}')
    return indices


def operations(page, key, original, records, timeout=120000):
    edits = {}
    def find(fragment):
        return matching_cells(key, original, fragment)[0]

    def find_all(fragment, count):
        return matching_cells(key, original, fragment, count=count)

    def state(index):
        return page.evaluate('''i => {const c=Jupyter.notebook.get_cell(i); return {
            frozen: !!(c.metadata.run_through_control && c.metadata.run_through_control.frozen),
            readOnly: !!c.code_mirror.getOption('readOnly'),
            message: c.last_msg_id || null, count: c.input_prompt_number,
            text: c.get_text(), outputs: c.output_area.outputs};}''', index)

    def select(index):
        cell = page.locator('.cell').nth(index)
        cell.locator('.input_prompt').scroll_into_view_if_needed()
        cell.locator('.input_prompt').click()
        page.keyboard.press('Escape')
        assert page.evaluate('() => Jupyter.notebook.get_selected_index()') == index

    def unfreeze(index):
        select(index)
        if state(index)['frozen']:
            page.locator('button:has(.fa-unfreeze)').click()
            assert not state(index)['frozen']
            records.append(dict(notebook=key, cell=index, action='unfreeze via toolbar'))
        if state(index)['readOnly']:
            page.locator('button:has(.fa-unlock-alt)').click()
            assert not state(index)['readOnly']

    def edit(index, text, reason):
        if text == state(index)['text']:
            raise ValueError(f'{key} cell {index}: edit made no change ({reason})')
        unfreeze(index)
        before = state(index)['text']
        select(index)
        page.keyboard.press('Enter')
        page.wait_for_function(
            'i => Jupyter.notebook.get_cell(i).code_mirror.hasFocus()', arg=index
        )
        page.keyboard.press('Control+A')
        page.keyboard.insert_text(text)
        assert state(index)['text'] == text, f'{key} cell {index}: editor content differs after input ({reason})'
        edits[index] = text
        records.append(dict(notebook=key, cell=index, action='edit working copy',
                            reason=reason, before=before, after=text))

    def run(index, inputs=(), *, inspect_error=False):
        unfreeze(index)
        select(index)
        before = state(index)['message']
        print(f'LC4RI EXEC: {key} cell {index}', flush=True)
        page.keyboard.press('Shift+Enter')
        for answer in inputs:
            prompt = page.locator('.cell').nth(index).locator('input.raw_input')
            prompt.wait_for(state='visible', timeout=60000)
            field = prompt.element_handle()
            prompt.fill(answer)
            prompt.press('Enter')
            field.wait_for_element_state('hidden')
        page.wait_for_function('''({i,before}) => {const c=Jupyter.notebook.get_cell(i);
            return c.last_msg_id && c.last_msg_id !== before && c.input_prompt_number !== '*' &&
                ((c.metadata.run_through_control && c.metadata.run_through_control.frozen) ||
                 c.output_area.outputs.some(o => o.output_type === 'error'));}''',
            arg=dict(i=index, before=before), timeout=timeout)
        observed = state(index)
        errors = [o for o in observed['outputs'] if o.get('output_type') == 'error']
        if errors and inspect_error:
            assert not observed['frozen'], observed
        else:
            assert not errors, errors
            assert observed['frozen'] and observed['readOnly'], observed
        records.append(dict(notebook=key, cell=index, action='Shift+Enter',
                            frozen=observed['frozen'],
                            execution_count=observed['count']))
        return observed

    def freeze_selected():
        page.locator('button:has(.fa-freeze)').click()

    return SimpleNamespace(freeze_selected=freeze_selected, find=find, find_all=find_all, state=state, select=select, unfreeze=unfreeze,
                           edit=edit, run=run, edits=edits)
