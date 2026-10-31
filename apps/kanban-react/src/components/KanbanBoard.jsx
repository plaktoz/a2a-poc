import { useRef } from 'react';
import BoardColumn from './BoardColumn';

export default function KanbanBoard({ board }) {
  const draggedTicketId = useRef(null);

  function startDrag(event, id) {
    draggedTicketId.current = id;
    event.dataTransfer?.setData('text/plain', id);
  }

  function endDrag() {
    draggedTicketId.current = null;
  }

  function drop(event, columnId) {
    event.preventDefault();
    if (draggedTicketId.current) board.moveTicket(draggedTicketId.current, columnId);
    endDrag();
  }

  return <div className="board">
    {board.columns.map((column) => <BoardColumn key={column.id} column={column} board={board}
      onDragStart={startDrag} onDragEnd={endDrag} onDrop={drop} />)}
  </div>;
}
