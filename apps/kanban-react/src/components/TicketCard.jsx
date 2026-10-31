import { useId, useState } from 'react';
import ColumnOptions from './ColumnOptions';
import TicketEditForm from './TicketEditForm';

export default function TicketCard({ ticket, board, onDragStart, onDragEnd }) {
  const id = useId();
  const [editing, setEditing] = useState(false);

  return <article className="ticket" aria-labelledby={`${id}-heading`} draggable="true"
    onDragStart={(event) => onDragStart(event, ticket.id)} onDragEnd={onDragEnd}>
    <h3 id={`${id}-heading`}>{ticket.title}</h3>
    {editing ? <TicketEditForm ticket={ticket} onCancel={() => setEditing(() => false)}
      onSave={(changes) => { board.updateTicket(ticket.id, changes); setEditing(() => false); }} /> : <>
      {ticket.description && <p className="description">{ticket.description}</p>}
      <div className="actions ticket-actions">
        <button className="text-button" type="button" onClick={() => setEditing(() => true)}>Edit ticket</button>
        <button className="text-button danger" type="button" onClick={() => board.deleteTicket(ticket.id)}>Delete ticket</button>
      </div>
    </>}
    <div className="field ticket-status"><label htmlFor={`${id}-status`}>Status</label>
      <select id={`${id}-status`} value={ticket.columnId} onChange={(event) => board.moveTicket(ticket.id, event.target.value)}>
        <ColumnOptions columns={board.columns} />
      </select>
    </div>
  </article>;
}
