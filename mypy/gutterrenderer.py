# ex:ts=4:et:

from gi.repository import Gdk, GtkSource


class GutterRenderer(GtkSource.GutterRenderer):
    def __init__(self, view):
        GtkSource.GutterRenderer.__init__(self)
        
        self.view = view
        
        self.set_size(8)
        # self.set_padding(3, 0)
        
        self.file_context = {}
    
    def get_messages_in_range(self, start, end):
        messages = []
        
        for msg in self.view.context_data:
            msg_start = None if msg.mark_start.get_deleted() else msg.buffer.get_iter_at_mark(msg.mark_start)
            msg_end = None if msg.mark_end.get_deleted() else msg.buffer.get_iter_at_mark(msg.mark_end)
            
            if msg_start and not msg_end:
                msg_end = msg_start
            elif msg_end and not msg_start:
                msg_start = msg_end
            elif not (msg_end or msg_start):
                continue
            
            if start.compare(msg_end) > 0 or end.compare(msg_start) < 0:
                continue
            
            messages.append(msg)
        
        return messages
    
    def do_draw(self, cr, bg_area, cell_area, start, end, state):
        GtkSource.GutterRenderer.do_draw(self, cr, bg_area, cell_area, start, end, state)
        
        messages = self.get_messages_in_range(start, end)
        if not messages:
            return
        
        level = max(m.level for m in messages)
        
        background = Gdk.RGBA()
        background.parse(level.color)
        Gdk.cairo_set_source_rgba(cr, background)
        cr.rectangle(cell_area.x, cell_area.y, cell_area.width, cell_area.height)
        cr.fill()
    
    def do_query_tooltip(self, it, area, x, y, tooltip):
        line = it.get_line() + 1
        messages = self.get_messages_in_range(it, it.get_buffer().get_iter_at_line(line))
        
        if not messages:
            return False
        
        text = "\n".join(message.get_pango_markup() for message in messages)
        tooltip.set_markup(f'<span font="monospace">{text}</span>')
        return True
    
    def update(self):
        self.queue_draw()

