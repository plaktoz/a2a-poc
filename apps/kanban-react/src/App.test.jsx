import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';

const column = (name) => screen.getByRole('region', { name, exact: true });
const ticket = (name) => screen.getByRole('article', { name, exact: true });
const button = (scope, name) => within(scope).getByRole('button', { name, exact: true });
const columnNames = () =>
  screen.getAllByRole('region').map((region) => within(region).getByRole('heading', { level: 2 }).textContent);

function expectOptions(select, names) {
  expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual(names);
}

function expectCount(region, count) {
  expect(within(region).getByText(count === 1 ? '1 ticket' : `${count} tickets`)).toBeVisible();
}

async function addColumn(user, name) {
  await user.type(screen.getByRole('textbox', { name: 'Column name', exact: true }), name);
  await user.click(screen.getByRole('button', { name: 'Add column', exact: true }));
}

async function addTicket(user, title, status) {
  await user.type(screen.getByRole('textbox', { name: 'Ticket title', exact: true }), title);
  if (status) {
    await user.selectOptions(screen.getByRole('combobox', { name: 'Initial status', exact: true }), status);
  }
  await user.click(screen.getByRole('button', { name: 'Add ticket', exact: true }));
}

async function renameColumn(user, name, replacement) {
  await user.click(button(column(name), 'Rename column'));
  await user.clear(within(column(name)).getByRole('textbox', { name: 'Column title', exact: true }));
  await user.type(
    within(column(name)).getByRole('textbox', { name: 'Column title', exact: true }),
    replacement,
  );
  await user.click(button(column(name), 'Save column'));
}

async function editTicket(user, name, title, description) {
  await user.click(button(ticket(name), 'Edit ticket'));
  const card = ticket(name);
  await user.clear(within(card).getByRole('textbox', { name: 'Title', exact: true }));
  await user.type(within(card).getByRole('textbox', { name: 'Title', exact: true }), title);
  await user.clear(within(card).getByRole('textbox', { name: 'Description', exact: true }));
  if (description) {
    await user.type(within(card).getByRole('textbox', { name: 'Description', exact: true }), description);
  }
  await user.click(button(card, 'Save ticket'));
}

function dragTicket(name, destination) {
  fireEvent.dragStart(ticket(name));
  fireEvent.dragOver(column(destination));
  fireEvent.drop(column(destination));
  fireEvent.dragEnd(ticket(name));
}

describe('Kanban board acceptance', () => {
  it('AC1: starts with ordered column regions labelled by their headings', () => {
    render(<App />);

    expect(columnNames()).toEqual(['To Do', 'In Progress', 'Done']);
    for (const name of ['To Do', 'In Progress', 'Done']) {
      expect(within(column(name)).getByRole('heading', { name, exact: true })).toBeVisible();
    }
  });

  it.each(['{broken JSON', 'null', '{}'])(
    'AC1: restores default columns when saved data is corrupt: %s',
    (saved) => {
      localStorage.setItem('kanban-board', saved);
      render(<App />);

      expect(columnNames()).toEqual(['To Do', 'In Progress', 'Done']);
      for (const name of ['To Do', 'In Progress', 'Done']) {
        expect(within(column(name)).getByRole('heading', { name, exact: true })).toBeVisible();
      }
    },
  );

  it('AC2: appends a trimmed column, updates initial status options and clears its input', async () => {
    const user = userEvent.setup();
    render(<App />);

    await addColumn(user, '  Review  ');

    expect(columnNames()).toEqual(['To Do', 'In Progress', 'Done', 'Review']);
    expect(within(column('Review')).getByRole('heading', { name: 'Review', exact: true })).toBeVisible();
    expectOptions(screen.getByRole('combobox', { name: 'Initial status', exact: true }), [
      'To Do', 'In Progress', 'Done', 'Review',
    ]);
    expect(screen.getByRole('textbox', { name: 'Column name', exact: true })).toHaveValue('');
  });

  it('AC2: ignores blank column names and rejects duplicate names regardless of case and spaces', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole('button', { name: 'Add column', exact: true }));
    await addColumn(user, '   ');
    expect(columnNames()).toEqual(['To Do', 'In Progress', 'Done']);

    await user.clear(screen.getByRole('textbox', { name: 'Column name', exact: true }));
    await addColumn(user, '  tO dO  ');

    expect(columnNames()).toEqual(['To Do', 'In Progress', 'Done']);
    expect(screen.getByRole('alert')).toHaveTextContent(/a column with that name already exists/i);
  });

  it('AC3: defaults to the first column and adds a trimmed ticket only to the selected column', async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(screen.getByRole('combobox', { name: 'Initial status', exact: true }))
      .toHaveDisplayValue('To Do');
    await addTicket(user, '  Write tests  ', 'In Progress');

    const card = within(column('In Progress')).getByRole('article', { name: 'Write tests', exact: true });
    expect(within(card).getByRole('heading', { name: 'Write tests', exact: true })).toBeVisible();
    expect(within(column('To Do')).queryByRole('article')).not.toBeInTheDocument();
    expect(within(column('Done')).queryByRole('article')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Ticket title', exact: true })).toHaveValue('');
  });

  it('AC3: ignores blank ticket titles and disables adding tickets when all columns are deleted', async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole('button', { name: 'Add ticket', exact: true }));
    await addTicket(user, '   ');
    expect(screen.queryByRole('article')).not.toBeInTheDocument();

    for (const name of ['To Do', 'In Progress', 'Done']) {
      await user.click(button(column(name), 'Delete column'));
    }
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add ticket', exact: true })).toBeDisabled();
  });

  it('AC4: lists all current columns and moves a ticket using its keyboard-accessible status select', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addColumn(user, 'Review');
    await addTicket(user, 'Keyboard task');

    const status = within(ticket('Keyboard task')).getByRole('combobox', { name: 'Status', exact: true });
    expectOptions(status, ['To Do', 'In Progress', 'Done', 'Review']);
    expect(status).toHaveDisplayValue('To Do');

    await user.selectOptions(status, 'Review');

    expect(within(column('Review')).getByRole('article', { name: 'Keyboard task', exact: true }))
      .toBeVisible();
    expect(within(column('To Do')).queryByRole('article')).not.toBeInTheDocument();
    expect(within(ticket('Keyboard task')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('Review');
  });

  it('AC5: supports native drag events without DataTransfer and prevents default on column dragover', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addTicket(user, 'Dragged task');

    expect(ticket('Dragged task')).toHaveAttribute('draggable', 'true');
    for (const name of ['To Do', 'In Progress', 'Done']) {
      expect(fireEvent.dragOver(column(name))).toBe(false);
    }

    dragTicket('Dragged task', 'Done');

    expect(within(column('Done')).getByRole('article', { name: 'Dragged task', exact: true }))
      .toBeVisible();
    expect(within(column('To Do')).queryByRole('article')).not.toBeInTheDocument();
    expect(within(ticket('Dragged task')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('Done');
  });

  it('AC5: accepts drops on descendants of the column region', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addTicket(user, 'Nested drop');

    fireEvent.dragStart(ticket('Nested drop'));
    const heading = within(column('In Progress')).getByRole('heading', { name: 'In Progress', exact: true });
    fireEvent.dragOver(heading);
    fireEvent.drop(heading);
    fireEvent.dragEnd(ticket('Nested drop'));

    expect(within(column('In Progress')).getByRole('article', { name: 'Nested drop', exact: true }))
      .toBeVisible();
    expect(within(ticket('Nested drop')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('In Progress');
  });

  it('AC5: leaves the board unchanged for own-column drops, empty drags and ended drags', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addTicket(user, 'Stay here');

    dragTicket('Stay here', 'To Do');
    fireEvent.dragOver(column('Done'));
    fireEvent.drop(column('Done'));

    fireEvent.dragStart(ticket('Stay here'));
    fireEvent.dragEnd(ticket('Stay here'));
    fireEvent.drop(column('In Progress'));

    expect(within(column('To Do')).getAllByRole('article')).toHaveLength(1);
    expect(within(column('To Do')).getByRole('article', { name: 'Stay here', exact: true })).toBeVisible();
    expect(within(column('Done')).queryByRole('article')).not.toBeInTheDocument();
    expect(within(column('In Progress')).queryByRole('article')).not.toBeInTheDocument();
    expect(within(ticket('Stay here')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('To Do');
  });

  it('AC6: deletes only the chosen ticket', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addTicket(user, 'Remove me');
    await addTicket(user, 'Same column');
    await addTicket(user, 'Other column', 'Done');

    await user.click(button(ticket('Remove me'), 'Delete ticket'));

    expect(screen.queryByRole('article', { name: 'Remove me', exact: true })).not.toBeInTheDocument();
    expect(within(column('To Do')).getByRole('article', { name: 'Same column', exact: true })).toBeVisible();
    expect(within(column('Done')).getByRole('article', { name: 'Other column', exact: true })).toBeVisible();
  });

  it('AC7: permits deleting only empty columns and removes their options from every select', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addTicket(user, 'First');
    await addTicket(user, 'Second', 'In Progress');

    expect(button(column('To Do'), 'Delete column')).toBeDisabled();
    expect(button(column('In Progress'), 'Delete column')).toBeDisabled();
    await user.click(button(column('To Do'), 'Delete column'));
    expect(column('To Do')).toBeVisible();

    expect(button(column('Done'), 'Delete column')).toBeEnabled();
    await user.click(button(column('Done'), 'Delete column'));

    expect(screen.queryByRole('region', { name: 'Done', exact: true })).not.toBeInTheDocument();
    expectOptions(screen.getByRole('combobox', { name: 'Initial status', exact: true }), [
      'To Do', 'In Progress',
    ]);
    for (const name of ['First', 'Second']) {
      expectOptions(within(ticket(name)).getByRole('combobox', { name: 'Status', exact: true }), [
        'To Do', 'In Progress',
      ]);
    }

    await user.click(button(ticket('First'), 'Delete ticket'));
    expect(button(column('To Do'), 'Delete column')).toBeEnabled();
    await user.click(button(column('To Do'), 'Delete column'));
    expect(columnNames()).toEqual(['In Progress']);
  });

  it('AC8: updates counts and empty hints when tickets are added, selected, dropped and deleted', async () => {
    const user = userEvent.setup();
    render(<App />);

    for (const name of ['To Do', 'In Progress', 'Done']) {
      expectCount(column(name), 0);
      expect(within(column(name)).getByText('No tickets yet')).toBeVisible();
    }

    await addTicket(user, 'One');
    await addTicket(user, 'Two');
    expectCount(column('To Do'), 2);
    expect(within(column('To Do')).queryByText('No tickets yet')).not.toBeInTheDocument();

    await user.selectOptions(
      within(ticket('One')).getByRole('combobox', { name: 'Status', exact: true }),
      'In Progress',
    );
    expectCount(column('To Do'), 1);
    expectCount(column('In Progress'), 1);
    expect(within(column('In Progress')).queryByText('No tickets yet')).not.toBeInTheDocument();

    dragTicket('Two', 'Done');
    expectCount(column('To Do'), 0);
    expect(within(column('To Do')).getByText('No tickets yet')).toBeVisible();
    expectCount(column('Done'), 1);
    expect(within(column('Done')).queryByText('No tickets yet')).not.toBeInTheDocument();

    await user.click(button(ticket('One'), 'Delete ticket'));
    expectCount(column('In Progress'), 0);
    expect(within(column('In Progress')).getByText('No tickets yet')).toBeVisible();
    await user.click(button(ticket('Two'), 'Delete ticket'));
    expectCount(column('Done'), 0);
    expect(within(column('Done')).getByText('No tickets yet')).toBeVisible();
  });

  it('AC9: edits a title and optional description in place, then allows clearing the description', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addTicket(user, 'Original', 'In Progress');

    await user.click(button(ticket('Original'), 'Edit ticket'));
    expect(within(ticket('Original')).getByRole('textbox', { name: 'Title', exact: true }))
      .toHaveValue('Original');
    const description = within(ticket('Original')).getByRole('textbox', { name: 'Description', exact: true });
    expect(description.tagName).toBe('TEXTAREA');
    expect(description).toHaveValue('');
    await user.click(button(ticket('Original'), 'Cancel'));

    await editTicket(user, 'Original', '  Updated  ', '  Helpful details  ');

    const card = within(column('In Progress')).getByRole('article', { name: 'Updated', exact: true });
    expect(within(card).getByRole('heading', { name: 'Updated', exact: true })).toBeVisible();
    expect(within(card).getByText('Helpful details', { exact: true })).toBeVisible();
    expect(within(card).queryByRole('textbox', { name: 'Title', exact: true })).not.toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: 'Save ticket', exact: true })).not.toBeInTheDocument();

    await user.click(button(card, 'Edit ticket'));
    expect(within(ticket('Updated')).getByRole('textbox', { name: 'Description', exact: true }))
      .toHaveValue('Helpful details');
    await user.clear(within(ticket('Updated')).getByRole('textbox', { name: 'Description', exact: true }));
    await user.click(button(ticket('Updated'), 'Save ticket'));

    expect(within(ticket('Updated')).queryByText('Helpful details')).not.toBeInTheDocument();
    expect(within(column('In Progress')).getByRole('article', { name: 'Updated', exact: true })).toBeVisible();
    await user.click(button(ticket('Updated'), 'Edit ticket'));
    expect(within(ticket('Updated')).getByRole('textbox', { name: 'Description', exact: true }))
      .toHaveValue('');
  });

  it.each(['', '   '])(
    'AC10: rejects an invalid edited title and cancels changes while retaining the saved label: %j',
    async (invalidTitle) => {
      const user = userEvent.setup();
      render(<App />);
      await addTicket(user, 'Saved title');
      await editTicket(user, 'Saved title', 'Saved title', 'Saved description');
      await user.click(button(ticket('Saved title'), 'Edit ticket'));

      const card = ticket('Saved title');
      await user.clear(within(card).getByRole('textbox', { name: 'Title', exact: true }));
      if (invalidTitle) {
        await user.type(within(card).getByRole('textbox', { name: 'Title', exact: true }), invalidTitle);
      }
      await user.clear(within(card).getByRole('textbox', { name: 'Description', exact: true }));
      await user.type(
        within(card).getByRole('textbox', { name: 'Description', exact: true }),
        'Unsaved description',
      );
      await user.click(button(card, 'Save ticket'));

      expect(ticket('Saved title')).toBeVisible();
      expect(within(card).getByRole('alert')).toHaveTextContent(/title is required/i);
      expect(within(card).getByRole('textbox', { name: 'Title', exact: true })).toBeVisible();

      await user.type(within(card).getByRole('textbox', { name: 'Title', exact: true }), 'Draft title');
      expect(ticket('Saved title')).toBeVisible();
      await user.click(button(card, 'Cancel'));

      expect(within(ticket('Saved title')).getByRole('heading', { name: 'Saved title', exact: true }))
        .toBeVisible();
      expect(within(ticket('Saved title')).getByText('Saved description')).toBeVisible();
      expect(within(ticket('Saved title')).queryByText('Unsaved description')).not.toBeInTheDocument();
      expect(within(ticket('Saved title')).queryByRole('textbox')).not.toBeInTheDocument();
    },
  );

  it('AC11: renames a column in place, retains its tickets and selected initial status, and updates all options', async () => {
    const user = userEvent.setup();
    render(<App />);
    await addTicket(user, 'Resident', 'In Progress');
    await addTicket(user, 'Neighbor', 'Done');
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Initial status', exact: true }),
      'In Progress',
    );

    await user.click(button(column('In Progress'), 'Rename column'));
    const region = column('In Progress');
    const input = within(region).getByRole('textbox', { name: 'Column title', exact: true });
    expect(input).toHaveValue('In Progress');
    await user.clear(input);
    await user.type(input, '  Working  ');
    expect(column('In Progress')).toBeVisible();
    expect(within(region).getByRole('heading', { name: 'In Progress', exact: true })).toBeVisible();
    await user.click(button(region, 'Save column'));

    expect(columnNames()).toEqual(['To Do', 'Working', 'Done']);
    expect(within(column('Working')).getByRole('article', { name: 'Resident', exact: true })).toBeVisible();
    expect(within(column('Working')).queryByRole('textbox', { name: 'Column title', exact: true }))
      .not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Initial status', exact: true }))
      .toHaveDisplayValue('Working');
    expectOptions(screen.getByRole('combobox', { name: 'Initial status', exact: true }), [
      'To Do', 'Working', 'Done',
    ]);
    for (const name of ['Resident', 'Neighbor']) {
      expectOptions(within(ticket(name)).getByRole('combobox', { name: 'Status', exact: true }), [
        'To Do', 'Working', 'Done',
      ]);
    }
    expect(within(ticket('Resident')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('Working');
    expect(within(ticket('Neighbor')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('Done');
  });

  it.each(['', '   ', '  tO dO  '])(
    'AC12: rejects invalid column names, explains the problem and allows cancelling: %j',
    async (replacement) => {
      const user = userEvent.setup();
      render(<App />);
      await user.click(button(column('Done'), 'Rename column'));
      const region = column('Done');
      const input = within(region).getByRole('textbox', { name: 'Column title', exact: true });
      await user.clear(input);
      if (replacement) await user.type(input, replacement);
      await user.click(button(region, 'Save column'));

      expect(columnNames()).toEqual(['To Do', 'In Progress', 'Done']);
      expect(input).toBeVisible();
      expect(within(region).getByRole('alert')).toHaveTextContent(
        replacement.trim() ? /a column with that name already exists/i : /column title is required/i,
      );

      await user.clear(input);
      await user.type(input, 'Discarded name');
      await user.click(button(region, 'Cancel'));
      expect(columnNames()).toEqual(['To Do', 'In Progress', 'Done']);
      expect(within(column('Done')).queryByRole('textbox', { name: 'Column title', exact: true }))
        .not.toBeInTheDocument();
    },
  );

  it('AC12: permits changing the case of a column’s own name', async () => {
    const user = userEvent.setup();
    render(<App />);

    await renameColumn(user, 'Done', 'DONE');

    expect(columnNames()).toEqual(['To Do', 'In Progress', 'DONE']);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(within(column('DONE')).queryByRole('textbox', { name: 'Column title', exact: true }))
      .not.toBeInTheDocument();
  });

  it('AC13: restores column changes, both kinds of moves, ticket edits, deletions, counts and empty hints', async () => {
    const user = userEvent.setup();
    const firstRender = render(<App />);

    await addColumn(user, 'Review');
    await addColumn(user, 'Archive');
    await renameColumn(user, 'In Progress', 'Working');
    await user.click(button(column('Done'), 'Delete column'));

    await addTicket(user, 'Select move');
    await addTicket(user, 'Drag move');
    await addTicket(user, 'Deleted ticket', 'Review');
    await addTicket(user, 'No description', 'Review');
    await addTicket(user, 'Clear description', 'Review');

    await user.selectOptions(
      within(ticket('Select move')).getByRole('combobox', { name: 'Status', exact: true }),
      'Working',
    );
    dragTicket('Drag move', 'Review');
    await editTicket(user, 'Select move', 'Selected and edited', 'Saved details');
    await editTicket(user, 'Drag move', 'Dragged and edited', 'Dragged details');
    await editTicket(user, 'Clear description', 'Clear description', 'Temporary details');
    await editTicket(user, 'Clear description', 'Clear description', '');
    await user.click(button(ticket('Deleted ticket'), 'Delete ticket'));

    expect(localStorage.getItem('kanban-board')).not.toBeNull();
    firstRender.unmount();
    render(<App />);

    expect(columnNames()).toEqual(['To Do', 'Working', 'Review', 'Archive']);
    expect(screen.queryByRole('region', { name: 'Done', exact: true })).not.toBeInTheDocument();
    expect(screen.queryByRole('article', { name: 'Deleted ticket', exact: true })).not.toBeInTheDocument();
    expect(screen.getAllByRole('article')).toHaveLength(4);

    expect(within(column('Working')).getByRole('article', { name: 'Selected and edited', exact: true }))
      .toBeVisible();
    expect(within(ticket('Selected and edited')).getByText('Saved details')).toBeVisible();
    expect(within(ticket('Selected and edited')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('Working');

    expect(within(column('Review')).getByRole('article', { name: 'Dragged and edited', exact: true }))
      .toBeVisible();
    expect(within(ticket('Dragged and edited')).getByText('Dragged details')).toBeVisible();
    expect(within(ticket('Dragged and edited')).getByRole('combobox', { name: 'Status', exact: true }))
      .toHaveDisplayValue('Review');
    expect(within(column('Review')).getByRole('article', { name: 'No description', exact: true }))
      .toBeVisible();
    expect(within(column('Review')).getByRole('article', { name: 'Clear description', exact: true }))
      .toBeVisible();
    expect(screen.queryByText('Temporary details')).not.toBeInTheDocument();

    expectCount(column('To Do'), 0);
    expectCount(column('Working'), 1);
    expectCount(column('Review'), 3);
    expectCount(column('Archive'), 0);
    expect(within(column('To Do')).getByText('No tickets yet')).toBeVisible();
    expect(within(column('Archive')).getByText('No tickets yet')).toBeVisible();
    expect(within(column('Working')).queryByText('No tickets yet')).not.toBeInTheDocument();
    expect(within(column('Review')).queryByText('No tickets yet')).not.toBeInTheDocument();

    expectOptions(screen.getByRole('combobox', { name: 'Initial status', exact: true }), [
      'To Do', 'Working', 'Review', 'Archive',
    ]);
    for (const name of ['Selected and edited', 'Dragged and edited', 'No description', 'Clear description']) {
      expectOptions(within(ticket(name)).getByRole('combobox', { name: 'Status', exact: true }), [
        'To Do', 'Working', 'Review', 'Archive',
      ]);
    }

    for (const name of ['No description', 'Clear description']) {
      await user.click(button(ticket(name), 'Edit ticket'));
      expect(within(ticket(name)).getByRole('textbox', { name: 'Description', exact: true }))
        .toHaveValue('');
      await user.click(button(ticket(name), 'Cancel'));
    }
  });

  it('AC13: restores an intentionally empty board without recreating the default columns', async () => {
    const user = userEvent.setup();
    const firstRender = render(<App />);

    for (const name of ['To Do', 'In Progress', 'Done']) {
      await user.click(button(column(name), 'Delete column'));
    }
    firstRender.unmount();
    render(<App />);

    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add ticket', exact: true })).toBeDisabled();
    expect(within(screen.getByRole('combobox', { name: 'Initial status', exact: true }))
      .queryAllByRole('option')).toHaveLength(0);
  });
});
