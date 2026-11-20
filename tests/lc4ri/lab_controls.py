"""Operate JupyterLab notebook cells through the visible controls."""
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import quote

from playwright.sync_api import expect


from notebook_controls import matching_cells, source

BASE = 'http://127.0.0.1:18888'


def open_document(page, base, token, filename):
    page.goto(base + '/lab/workspaces/' + uuid4().hex + '/tree/' + quote(filename) + '?token=' + token)
    page.wait_for_function('name => window.jupyterapp?.shell.currentWidget?.context?.path === name',
                           arg=filename, timeout=120000)


def open_working_copy(page, base, token, template, document):
    open_document(page, base, token, template)
    expect(page.get_by_title('unfreeze selected cells', exact=True)).to_be_visible()
    page.get_by_text('File', exact=True).and_(page.locator('.lm-MenuBar-itemLabel')).click()
    page.locator('.lm-Menu-item[data-command="docmanager:save-as"]:visible').click()
    page.locator('.jp-Dialog input').fill(document)
    page.locator('.jp-Dialog button.jp-mod-accept').click()
    page.wait_for_function("""name => window.jupyterapp.shell.currentWidget.context.path === name ||
        Array.from(document.querySelectorAll('.jp-Dialog')).some(d => d.textContent.includes('Save your work'))""",
        arg=document)
    dialog = page.locator('.jp-Dialog')
    if dialog.is_visible():
        expect(dialog).to_contain_text('Save your work')
        dialog.get_by_role('button', name='Discard changes to file', exact=True).click()
    page.get_by_text(document, exact=True).and_(page.locator('.lm-TabBar-tabLabel')).click()
    page.wait_for_function('name => window.jupyterapp.shell.currentWidget.context.path === name', arg=document)
    page.evaluate('async () => {await window.jupyterapp.shell.currentWidget.sessionContext.ready;}')
    page.wait_for_function('window.jupyterapp.shell.currentWidget.sessionContext.session.kernel.connectionStatus === "connected"',
                           timeout=120000)
    page.get_by_text('Edit', exact=True).and_(page.locator('.lm-MenuBar-itemLabel')).click()
    page.locator('.lm-Menu-item[data-command="editmenu:clear-all"]:visible').click()
    page.wait_for_function("window.jupyterapp.shell.currentWidget.context.model.toJSON().cells.filter(c => c.cell_type === 'code').every(c => c.outputs.length === 0)")



def operations(page, key, reference, records, timeout=120000):
    edits = {}

    def find(fragment):
        return matching_cells(key, reference, fragment)[0]

    def find_all(fragment, count):
        return matching_cells(key, reference, fragment, count=count)

    def state(index):
        return page.evaluate('''i => {
            const cell = window.jupyterapp.shell.currentWidget.content.widgets[i];
            const flags = cell.model.getMetadata('run_through_control');
            return {frozen: !!(flags && flags.frozen),
                readOnly: cell.model.getMetadata('editable') === false,
                count: cell.model.executionCount,
                text: cell.model.sharedModel.getSource(), outputs: cell.model.outputs.toJSON()};
        }''', index)

    def select(index):
        page.evaluate('async i => {await window.jupyterapp.shell.currentWidget.content.scrollToItem(i);}', index)
        cell = page.locator(f'.jp-NotebookPanel:visible .jp-Cell[data-windowed-list-index="{index}"]')
        cell.locator('.jp-InputArea-prompt').click()
        page.keyboard.press('Escape')
        assert page.evaluate('window.jupyterapp.shell.currentWidget.content.activeCellIndex') == index
        return cell

    def unfreeze(index):
        select(index)
        if state(index)['frozen']:
            page.get_by_title('unfreeze selected cells', exact=True).click()
            assert not state(index)['frozen']
        if state(index)['readOnly']:
            page.get_by_title('make selected cells editable', exact=True).click()
            assert not state(index)['readOnly']

    def edit(index, text, reason):
        if text == state(index)['text']:
            raise ValueError(f'{key} cell {index}: edit made no change ({reason})')
        unfreeze(index)
        before = state(index)['text']
        select(index).locator('.cm-content').click()
        page.keyboard.press('Control+A')
        page.keyboard.insert_text(text)
        assert state(index)['text'] == text
        edits[index] = text
        records.append(dict(notebook=key, cell=index, action='edit input', reason=reason,
                            before=before, after=text))

    def run(index, inputs=(), *, inspect_error=False, timeout_ms=timeout):
        unfreeze(index)
        cell = select(index)
        before = state(index)['count']
        print(f'LC4RI EXEC: {key} cell {index}', flush=True)
        page.keyboard.press('Shift+Enter')
        prompts = cell.locator('.jp-Stdin-input:visible:not([data-ci-submitted])')
        for answer in inputs:
            prompts.wait_for(timeout=60000)
            field = prompts.element_handle()
            field.fill(answer)
            field.evaluate("field => field.dataset.ciSubmitted = 'true'")
            field.press('Enter')
        page.wait_for_function('''({i,before}) => {
            const panel = window.jupyterapp.shell.currentWidget;
            const cell = panel.content.widgets[i];
            const flags = cell.model.getMetadata('run_through_control');
            return cell.model.executionCount !== null && cell.model.executionCount !== before &&
                panel.sessionContext.session.kernel.status === 'idle' &&
                ((flags && flags.frozen) || cell.model.outputs.toJSON().some(o => o.output_type === 'error'));
        }''', arg=dict(i=index, before=before), timeout=timeout_ms)
        observed = state(index)
        errors = [output for output in observed['outputs'] if output['output_type'] == 'error']
        records.append(dict(notebook=key, cell=index, action='Shift+Enter',
                            errors=errors, execution_count=observed['count']))
        if errors:
            for output in observed['outputs']:
                if output['output_type'] == 'stream':
                    print(output['text'], end='', flush=True)
        if errors and inspect_error:
            assert not observed['frozen']
        else:
            assert not errors, errors
            assert observed['frozen'] and observed['readOnly'], observed
        return observed

    def freeze_selected():
        page.get_by_title('freeze selected cells', exact=True).click()

    return SimpleNamespace(freeze_selected=freeze_selected, find=find, find_all=find_all, state=state, select=select, edit=edit, run=run, edits=edits)
