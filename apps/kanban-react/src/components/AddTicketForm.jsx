import { useId, useState } from 'react';
import ColumnOptions from './ColumnOptions';

export default function AddTicketForm({ columns, addTicket }) {
  const id = useId();
  const [title, setTitle] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const columnId = columns.some((column) => column.id === selectedId) ? selectedId : columns[0]?.id ?? '';

  function submit(event) {
    event.preventDefault();
    if (!title.trim() || !columnId) return;
    addTicket(title, columnId);
    setTitle(() => '');
  }

  return <form className="creation-form ticket-creation" onSubmit={submit}>
    <div className="field"><label htmlFor={`${id}-title`}>Ticket title</label>
      <input id={`${id}-title`} value={title} onChange={(event) => setTitle(() => event.target.value)} placeholder="What needs to get done?" />
    </div>
    <div className="field"><label htmlFor={`${id}-status`}>Initial status</label>
      <select id={`${id}-status`} value={columnId} onChange={(event) => setSelectedId(() => event.target.value)}>
        <ColumnOptions columns={columns} />
      </select>
    </div>
    <button type="submit" disabled={!columns.length}>Add ticket</button>
  </form>;
}
