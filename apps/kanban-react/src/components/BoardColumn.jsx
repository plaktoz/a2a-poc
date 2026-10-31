import { useId, useState } from 'react';
import RenameColumnForm from './RenameColumnForm';
import TicketCard from './TicketCard';

export default function BoardColumn({ column, board, onDrop, onDragStart, onDragEnd }) {
  const headingId = useId();
  const [renaming, setRenaming] = useState(false);
  const tickets = board.tickets.filter((ticket) => ticket.columnId === column.id);

  return <section className="column" aria-labelledby={headingId}
    onDragOver={(event) => event.preventDefault()} onDrop={(event) => onDrop(event, column.id)}>
    <header className="column-header">
      <div className="column-title"><h2 id={headingId}>{column.title}</h2>
        <span className="count">{tickets.length} {tickets.length === 1 ? 'ticket' : 'tickets'}</span>
      </div>
      <div className="actions">
        <button className="text-button" type="button" onClick={() => setRenaming(() => true)}>Rename column</button>
        <button className="text-button danger" type="button" disabled={tickets.length > 0} onClick={() => board.deleteColumn(column.id)}>Delete column</button>
      </div>
    </header>
    {renaming && <RenameColumnForm column={column} columnTitleError={board.columnTitleError}
      onCancel={() => setRenaming(() => false)}
      onSave={(title) => { board.renameColumn(column.id, title); setRenaming(() => false); }} />}
    <div className="ticket-list">
      {tickets.length === 0 ? <p className="empty-hint">No tickets yet</p> : tickets.map((ticket) =>
        <TicketCard key={ticket.id} ticket={ticket} board={board} onDragStart={onDragStart} onDragEnd={onDragEnd} />)}
    </div>
  </section>;
}
