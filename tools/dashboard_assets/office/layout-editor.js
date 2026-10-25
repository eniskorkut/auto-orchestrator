/* Auto-Orchestrator — grid-snapped office layout editor.
 *
 * attach({canvas, scene, getLayout, onChange, onSave, onReset}) -> {enable, destroy}
 *
 * Editing is layout-only: rooms move with their door and desks, doors slide
 * along a room wall, desks move inside their room. Evidence/agent state is
 * never read or mutated.
 *
 * Pointer -> tile resolution order:
 *   1. options.toTile(px, py)                  (canvas-relative CSS pixels)
 *   2. scene.clientToTile(clientX, clientY)    (office-scene hook)
 *   3. scene.screenToTile(px, py)              (canvas-relative CSS pixels)
 *   4. scene.getCamera() / fit-to-canvas fallback
 *
 * Optional extras: projectId and theme for default save/reset persistence,
 * onError(message), enabled(true) to start in edit mode.
 */
(function (root) {
  'use strict';

  function layoutsApi() {
    return root.AOOfficeLayouts || null;
  }

  function numberOrNull(value) {
    return typeof value === 'number' && isFinite(value) ? value : null;
  }

  function firstNumber() {
    for (var i = 0; i < arguments.length; i += 1) {
      var value = numberOrNull(arguments[i]);
      if (value != null) return value;
    }
    return null;
  }

  function clone(value) {
    var api = layoutsApi();
    if (api && typeof api.clone === 'function') return api.clone(value);
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function serial(value) {
    var api = layoutsApi();
    if (api && typeof api.serialize === 'function') return api.serialize(value);
    return JSON.stringify(value);
  }

  function isEditableTarget(target) {
    if (!target || typeof target !== 'object') return false;
    var tag = target.tagName ? String(target.tagName).toLowerCase() : '';
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'option') return true;
    return target.isContentEditable === true;
  }

  function attach(options) {
    var opts = options && typeof options === 'object' ? options : {};
    if (!opts.canvas || typeof opts.canvas.getContext !== 'function') {
      throw new Error('AOLayoutEditor.attach requires a canvas element');
    }

    var canvas = opts.canvas;
    var scene = opts.scene || null;
    var getLayout = typeof opts.getLayout === 'function'
      ? opts.getLayout
      : (scene && typeof scene.getLayout === 'function' ? function () { return scene.getLayout(); } : function () { return null; });
    var onChange = typeof opts.onChange === 'function' ? opts.onChange : null;
    var onSave = typeof opts.onSave === 'function' ? opts.onSave : null;
    var onReset = typeof opts.onReset === 'function' ? opts.onReset : null;
    var onError = typeof opts.onError === 'function' ? opts.onError : null;
    var projectId = opts.projectId;
    var theme = opts.theme;
    var documentRef = typeof root.document !== 'undefined' ? root.document : null;

    var enabled = false;
    var destroyed = false;
    var current = null;
    var lastEmittedSerial = null;
    var staleBaseSerial = null;
    var selection = null;
    var drag = null;
    var lastError = null;
    var loopRaf = 0;
    var savedCursor = null;
    var listenersOn = false;

    function reportError(message) {
      lastError = message || null;
      if (onError) {
        try { onError(lastError); } catch (error) { /* callbacks must not break editing */ }
      }
    }

    function api() {
      return layoutsApi();
    }

    function findRoom(layout, id) {
      if (!layout || !Array.isArray(layout.rooms)) return null;
      for (var i = 0; i < layout.rooms.length; i += 1) {
        if (layout.rooms[i] && layout.rooms[i].id === id) return layout.rooms[i];
      }
      return null;
    }

    function findDesk(layout, id) {
      if (!layout || !Array.isArray(layout.desks)) return null;
      for (var i = 0; i < layout.desks.length; i += 1) {
        if (layout.desks[i] && layout.desks[i].id === id) return layout.desks[i];
      }
      return null;
    }

    function readExternal() {
      try {
        return getLayout();
      } catch (error) {
        return null;
      }
    }

    /* Keep an internal copy so a scene that applies layout changes on the next
     * frame does not make the editor fight itself. External changes (theme,
     * reset, project switch) are adopted. */
    function layout() {
      var external = readExternal();
      if (!external || typeof external !== 'object') return current;
      if (!current) {
        current = clone(external);
        return current;
      }
      var externalSerial = serial(external);
      if (externalSerial === serial(current)) {
        staleBaseSerial = null;
        return current;
      }
      if (lastEmittedSerial && externalSerial === lastEmittedSerial) return current;
      if (staleBaseSerial && externalSerial === staleBaseSerial) return current;
      current = clone(external);
      lastEmittedSerial = null;
      staleBaseSerial = null;
      if (selection) {
        var stillThere = selection.kind === 'room'
          ? findRoom(current, selection.id)
          : findDesk(current, selection.id);
        if (!stillThere) selection = null;
      }
      return current;
    }

    function emit(layout) {
      lastEmittedSerial = serial(layout);
      if (!onChange) return;
      try {
        onChange(clone(layout));
      } catch (error) {
        reportError(error && error.message ? error.message : 'layout change handler failed');
      }
    }

    function setCurrent(layout) {
      current = layout;
      emit(current);
    }

    function rectOf() {
      if (typeof canvas.getBoundingClientRect === 'function') return canvas.getBoundingClientRect();
      return { left: 0, top: 0, width: canvas.width || 0, height: canvas.height || 0 };
    }

    function sceneCamera() {
      if (!scene || typeof scene.getCamera !== 'function') return null;
      try {
        var camera = scene.getCamera();
        return camera && typeof camera === 'object' ? camera : null;
      } catch (error) {
        return null;
      }
    }

    function projection(layout, rect) {
      var projection = { scale: layout.tile, offsetX: 0, offsetY: 0 };
      var camera = sceneCamera();
      if (camera) {
        var zoom = firstNumber(camera.zoom, camera.k);
        var scale = numberOrNull(camera.scale);
        var tilePixels = firstNumber(camera.tileSize, camera.tilePx, camera.tile);
        if (scale != null && scale > 0 && (zoom != null || numberOrNull(camera.baseScale) != null)) {
          /* office-scene exposes world-pixels-per-tile as camera.scale. */
          projection.scale = layout.tile * scale;
        } else {
          if (zoom == null || zoom <= 0) zoom = 1;
          if (tilePixels == null || tilePixels <= 0) tilePixels = layout.tile;
          projection.scale = tilePixels * zoom;
        }
        if (numberOrNull(camera.offsetX) != null || numberOrNull(camera.offsetY) != null) {
          projection.offsetX = firstNumber(camera.offsetX, 0);
          projection.offsetY = firstNumber(camera.offsetY, 0);
        } else if (numberOrNull(camera.originX) != null || numberOrNull(camera.originY) != null) {
          projection.offsetX = firstNumber(camera.originX, 0);
          projection.offsetY = firstNumber(camera.originY, 0);
        } else {
          projection.offsetX = firstNumber(camera.x, camera.panX, 0);
          projection.offsetY = firstNumber(camera.y, camera.panY, 0);
        }
      } else if (rect.width > 0 && rect.height > 0) {
        projection.scale = Math.max(1, Math.min(rect.width / layout.width, rect.height / layout.height));
        projection.offsetX = (rect.width - layout.width * projection.scale) / 2;
        projection.offsetY = (rect.height - layout.height * projection.scale) / 2;
      }
      if (!(projection.scale > 0)) projection.scale = layout.tile;
      return projection;
    }

    function normalizeTile(value) {
      if (!value) return null;
      var x = null;
      var y = null;
      if (Array.isArray(value)) {
        x = value[0];
        y = value[1];
      } else if (typeof value === 'object') {
        x = value.x;
        y = value.y;
      }
      if (numberOrNull(x) == null || numberOrNull(y) == null) return null;
      return { x: Math.floor(x), y: Math.floor(y) };
    }

    function resolveTile(clientX, clientY) {
      var layoutValue = layout();
      if (!layoutValue) return null;
      var rect = rectOf();
      var px = clientX - rect.left;
      var py = clientY - rect.top;
      try {
        if (typeof opts.toTile === 'function') {
          var custom = normalizeTile(opts.toTile(px, py));
          if (custom) return custom;
        } else if (scene && typeof scene.clientToTile === 'function') {
          var clientTile = normalizeTile(scene.clientToTile(clientX, clientY));
          if (clientTile) return clientTile;
        } else if (scene && typeof scene.screenToTile === 'function') {
          /* Canvas-relative CSS pixels, as documented by office-scene. */
          var sceneTile = normalizeTile(scene.screenToTile(px, py));
          if (sceneTile) return sceneTile;
        }
      } catch (error) {
        reportError(error && error.message ? error.message : 'tile projection failed');
      }
      var projectionValue = projection(layoutValue, rect);
      return {
        x: Math.floor((px - projectionValue.offsetX) / projectionValue.scale),
        y: Math.floor((py - projectionValue.offsetY) / projectionValue.scale)
      };
    }

    function inBounds(layout, tile) {
      return tile && tile.x >= 0 && tile.y >= 0 && tile.x < layout.width && tile.y < layout.height;
    }

    function hitTest(layout, tile, preferRoom) {
      var apiRef = api();
      if (!tile || !inBounds(layout, tile)) return null;
      var rooms = Array.isArray(layout.rooms) ? layout.rooms : [];
      for (var r = 0; r < rooms.length; r += 1) {
        var room = rooms[r];
        if (room && room.door && room.door.x === tile.x && room.door.y === tile.y) {
          return { kind: 'door', id: room.id };
        }
      }
      if (!preferRoom && apiRef && typeof apiRef.deskFor === 'function') {
        var desk = apiRef.deskFor(layout, tile.x, tile.y);
        if (desk) return { kind: 'desk', id: desk.id };
      }
      var room = apiRef && typeof apiRef.roomAt === 'function'
        ? apiRef.roomAt(layout, tile.x, tile.y)
        : null;
      if (room) return { kind: 'room', id: room.id };
      return null;
    }

    /* The scene stops its render loop while editing. Ask it for one repaint
     * when our overlay disappears, otherwise an old highlight stays visible. */
    function refreshScene() {
      if (!scene || !enabled) return;
      try {
        if (typeof scene.editMode === 'function') scene.editMode(true);
        else if (typeof scene.resize === 'function') scene.resize();
      } catch (error) { /* optional scene hook */ }
    }

    function requestDraw() {
      if (destroyed || typeof root.requestAnimationFrame !== 'function') return;
      if (loopRaf) return;
      function tick() {
        loopRaf = 0;
        draw();
        if (!destroyed && enabled && (selection || drag)) {
          loopRaf = root.requestAnimationFrame(tick);
        }
      }
      loopRaf = root.requestAnimationFrame(tick);
    }

    function roomBounds(layout, room) {
      return { x: room.x, y: room.y, w: room.w, h: room.h };
    }

    function drawTile(ctx, projectionValue, tile, color, dash) {
      if (!tile) return;
      var size = projectionValue.scale;
      var x = projectionValue.offsetX + tile.x * size;
      var y = projectionValue.offsetY + tile.y * size;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      if (dash) ctx.setLineDash([4, 3]);
      ctx.strokeRect(x + 1, y + 1, Math.max(1, size - 2), Math.max(1, size - 2));
      ctx.restore();
    }

    function drawOutline(ctx, projectionValue, rect, color) {
      var size = projectionValue.scale;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.setLineDash([]);
      ctx.strokeRect(
        projectionValue.offsetX + rect.x * size + 1,
        projectionValue.offsetY + rect.y * size + 1,
        Math.max(1, rect.w * size - 2),
        Math.max(1, rect.h * size - 2)
      );
      ctx.restore();
    }

    function draw() {
      if (!enabled || destroyed) return;
      var layoutValue = current;
      if (!layoutValue || !selection) return;
      var ctx = null;
      try {
        ctx = canvas.getContext('2d');
      } catch (error) {
        ctx = null;
      }
      if (!ctx) return;

      var rect = rectOf();
      var projectionValue = projection(layoutValue, rect);
      var target = null;
      var error = Boolean(lastError);

      if (drag) {
        var baseRoom = drag.kind === 'room' ? findRoom(drag.base, drag.id) : null;
        var baseDesk = drag.kind === 'desk' ? findDesk(drag.base, drag.id) : null;
        if (baseRoom) {
          target = {
            kind: 'room',
            x: baseRoom.x + drag.lastDx,
            y: baseRoom.y + drag.lastDy,
            w: baseRoom.w,
            h: baseRoom.h
          };
        } else if (baseDesk) {
          target = {
            kind: 'desk',
            x: baseDesk.x + drag.lastDx,
            y: baseDesk.y + drag.lastDy,
            seat: baseDesk.seat ? { x: baseDesk.seat.x + drag.lastDx, y: baseDesk.seat.y + drag.lastDy } : null
          };
        }
      }

      var scaleX = rect.width > 0 ? canvas.width / rect.width : 1;
      var scaleY = rect.height > 0 ? canvas.height / rect.height : 1;
      ctx.save();
      if (typeof ctx.setTransform === 'function') ctx.setTransform(scaleX, 0, 0, scaleY, 0, 0);
      ctx.globalAlpha = 1;

      if (selection.kind === 'room') {
        var selectedRoom = findRoom(layoutValue, selection.id);
        var room = target && target.kind === 'room' ? target : (selectedRoom ? roomBounds(layoutValue, selectedRoom) : null);
        if (room) drawOutline(ctx, projectionValue, room, error ? '#f87171' : '#7dd3fc');
        if (selectedRoom && selectedRoom.door && !target) {
          drawTile(ctx, projectionValue, selectedRoom.door, '#fbbf24', false);
        }
      } else if (selection.kind === 'desk') {
        var selectedDesk = findDesk(layoutValue, selection.id);
        var deskRect = null;
        var seat = null;
        if (target && target.kind === 'desk') {
          deskRect = { x: target.x, y: target.y, w: 1, h: 1 };
          seat = target.seat;
        } else if (selectedDesk) {
          deskRect = { x: selectedDesk.x, y: selectedDesk.y, w: 1, h: 1 };
          seat = selectedDesk.seat;
        }
        if (deskRect) drawOutline(ctx, projectionValue, deskRect, error ? '#f87171' : '#7dd3fc');
        drawTile(ctx, projectionValue, seat, '#34d399', false);
      }

      ctx.restore();
    }

    function beginDrag(kind, id, tile) {
      var layoutValue = layout();
      if (!layoutValue) return;
      var base = clone(layoutValue);
      drag = {
        kind: kind,
        id: id,
        start: { x: tile.x, y: tile.y },
        base: base,
        baseSerial: serial(base),
        lastDx: 0,
        lastDy: 0,
        moved: false,
        pointerId: null
      };
      staleBaseSerial = drag.baseSerial;
      lastError = null;
      requestDraw();
    }

    function onPointerDown(event) {
      if (!enabled || destroyed) return;
      if (event.isPrimary === false) return;
      if (event.button != null && event.button !== 0) return;
      var layoutValue = layout();
      if (!layoutValue) return;
      var tile = resolveTile(event.clientX, event.clientY);
      if (!tile || !inBounds(layoutValue, tile)) return;
      var hit = hitTest(layoutValue, tile, Boolean(event.altKey));
      event.preventDefault();
      if (!hit) {
        selection = null;
        drag = null;
        lastError = null;
        refreshScene();
        requestDraw();
        return;
      }
      selection = { kind: hit.kind === 'door' ? 'room' : hit.kind, id: hit.id };
      beginDrag(hit.kind, hit.id, tile);
      if (drag) {
        drag.pointerId = event.pointerId == null ? null : event.pointerId;
        if (drag.pointerId != null && typeof canvas.setPointerCapture === 'function') {
          try { canvas.setPointerCapture(drag.pointerId); } catch (error) { /* optional */ }
        }
      }
      canvas.style.cursor = 'grabbing';
      requestDraw();
    }

    function onPointerMove(event) {
      if (!enabled || destroyed) return;
      if (!drag) {
        if (event.pointerType === 'mouse' || event.pointerType == null) {
          var hoverLayout = layout();
          var hoverTile = hoverLayout ? resolveTile(event.clientX, event.clientY) : null;
          var hover = hoverLayout && hoverTile ? hitTest(hoverLayout, hoverTile, Boolean(event.altKey)) : null;
          canvas.style.cursor = hover ? 'grab' : 'default';
        }
        if (selection) requestDraw();
        return;
      }
      var tile = resolveTile(event.clientX, event.clientY);
      if (!tile) return;
      var dx = tile.x - drag.start.x;
      var dy = tile.y - drag.start.y;
      if (dx === drag.lastDx && dy === drag.lastDy) return;
      drag.lastDx = dx;
      drag.lastDy = dy;
      var apiRef = api();
      if (!apiRef) return;
      var result = drag.kind === 'room'
        ? apiRef.moveRoom(drag.base, drag.id, dx, dy)
        : drag.kind === 'door'
          ? apiRef.moveDoor(drag.base, drag.id, tile.x, tile.y)
          : apiRef.moveDesk(drag.base, drag.id, dx, dy);
      if (result && result.ok) {
        drag.moved = dx !== 0 || dy !== 0;
        lastError = null;
        setCurrent(result.layout);
      } else {
        reportError(result && result.errors && result.errors.length ? result.errors[0] : 'placement rejected');
      }
      event.preventDefault();
      requestDraw();
    }

    function endDrag(commit) {
      if (!drag) return;
      if (!commit) {
        setCurrent(drag.base);
        staleBaseSerial = null;
      } else {
        staleBaseSerial = drag.baseSerial;
      }
      if (drag.pointerId != null && typeof canvas.releasePointerCapture === 'function') {
        try { canvas.releasePointerCapture(drag.pointerId); } catch (error) { /* optional */ }
      }
      drag = null;
      lastError = null;
      canvas.style.cursor = enabled ? 'grab' : (savedCursor || '');
      requestDraw();
    }

    function onPointerUp(event) {
      if (!drag) return;
      if (drag.pointerId != null && event && event.pointerId != null && event.pointerId !== drag.pointerId) return;
      endDrag(true);
    }

    function onPointerCancel() {
      if (drag) endDrag(false);
    }

    function applyArrow(dx, dy) {
      if (!selection) return false;
      var layoutValue = layout();
      if (!layoutValue) return false;
      var apiRef = api();
      if (!apiRef) return false;
      var previousSerial = serial(layoutValue);
      var result = selection.kind === 'room'
        ? apiRef.moveRoom(layoutValue, selection.id, dx, dy)
        : apiRef.moveDesk(layoutValue, selection.id, dx, dy);
      if (result && result.ok) {
        lastError = null;
        staleBaseSerial = previousSerial;
        setCurrent(result.layout);
        requestDraw();
        return true;
      }
      reportError(result && result.errors && result.errors.length ? result.errors[0] : 'placement rejected');
      requestDraw();
      return false;
    }

    function save() {
      var layoutValue = layout();
      if (!layoutValue) return false;
      var apiRef = api();
      if (!apiRef || !apiRef.validate(layoutValue).ok) {
        reportError('cannot save an invalid layout');
        return false;
      }
      if (onSave) {
        try {
          var result = onSave(clone(layoutValue));
          if (result !== false) lastError = null;
          return result !== false;
        } catch (error) {
          reportError(error && error.message ? error.message : 'save handler failed');
          return false;
        }
      }
      if (projectId != null && apiRef && typeof apiRef.save === 'function') {
        var saved = apiRef.save(projectId, layoutValue);
        if (!saved) reportError('layout could not be stored');
        else lastError = null;
        return Boolean(saved);
      }
      return false;
    }

    function reset() {
      if (onReset) {
        try {
          var resetResult = onReset();
          selection = null;
          drag = null;
          current = null;
          lastEmittedSerial = null;
          staleBaseSerial = null;
          lastError = null;
          refreshScene();
          requestDraw();
          return resetResult !== false;
        } catch (error) {
          reportError(error && error.message ? error.message : 'reset handler failed');
          return false;
        }
      }
      if (projectId == null) return false;
      var apiRef = api();
      if (!apiRef) return false;
      apiRef.reset(projectId);
      var next = apiRef.load(projectId, theme || (current && current.theme));
      if (!next) return false;
      lastEmittedSerial = null;
      staleBaseSerial = null;
      selection = null;
      drag = null;
      current = clone(next);
      emit(current);
      requestDraw();
      return true;
    }

    function cancel() {
      if (drag) endDrag(false);
      selection = null;
      lastError = null;
      refreshScene();
      requestDraw();
    }

    function onKeyDown(event) {
      if (!enabled || destroyed) return;
      if (isEditableTarget(event.target)) return;
      var key = event.key;
      if (key === 'Escape') {
        if (drag) {
          onPointerCancel();
        } else {
          selection = null;
          lastError = null;
          refreshScene();
          requestDraw();
        }
        event.preventDefault();
        return;
      }
      if ((event.metaKey || event.ctrlKey) && (key === 's' || key === 'S')) {
        event.preventDefault();
        save();
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      var dx = 0;
      var dy = 0;
      if (key === 'ArrowLeft') dx = -1;
      else if (key === 'ArrowRight') dx = 1;
      else if (key === 'ArrowUp') dy = -1;
      else if (key === 'ArrowDown') dy = 1;
      if (!dx && !dy) return;
      if (!selection) return;
      if (applyArrow(dx, dy)) event.preventDefault();
    }

    function attachListeners() {
      if (listenersOn) return;
      listenersOn = true;
      canvas.addEventListener('pointerdown', onPointerDown, { passive: false });
      canvas.addEventListener('pointermove', onPointerMove, { passive: false });
      canvas.addEventListener('pointerup', onPointerUp);
      canvas.addEventListener('pointercancel', onPointerCancel);
      if (documentRef && typeof documentRef.addEventListener === 'function') {
        documentRef.addEventListener('keydown', onKeyDown);
      }
    }

    function detachListeners() {
      if (!listenersOn) return;
      listenersOn = false;
      canvas.removeEventListener('pointerdown', onPointerDown, { passive: false });
      canvas.removeEventListener('pointermove', onPointerMove, { passive: false });
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('pointercancel', onPointerCancel);
      if (documentRef && typeof documentRef.removeEventListener === 'function') {
        documentRef.removeEventListener('keydown', onKeyDown);
      }
    }

    function setSceneEditMode(value) {
      if (!scene || typeof scene.editMode !== 'function') return;
      try { scene.editMode(Boolean(value)); } catch (error) { /* optional scene hook */ }
    }

    function enable(value) {
      if (destroyed) return instance;
      var next = Boolean(value);
      if (next === enabled) {
        if (next) {
          canvas.style.cursor = 'grab';
          requestDraw();
        }
        return instance;
      }
      enabled = next;
      if (enabled) {
        savedCursor = canvas.style.cursor || '';
        current = clone(readExternal());
        lastEmittedSerial = null;
        staleBaseSerial = null;
        selection = null;
        drag = null;
        lastError = null;
        attachListeners();
        canvas.style.cursor = 'grab';
        if (loopRaf && typeof root.cancelAnimationFrame === 'function') root.cancelAnimationFrame(loopRaf);
        loopRaf = 0;
        setSceneEditMode(true);
        requestDraw();
      } else {
        if (drag) {
          setCurrent(drag.base);
          drag = null;
        }
        selection = null;
        lastError = null;
        detachListeners();
        canvas.style.cursor = savedCursor || '';
        if (loopRaf && typeof root.cancelAnimationFrame === 'function') root.cancelAnimationFrame(loopRaf);
        loopRaf = 0;
        setSceneEditMode(false);
      }
      return instance;
    }

    function destroy() {
      if (destroyed) return;
      enable(false);
      destroyed = true;
      detachListeners();
      if (loopRaf && typeof root.cancelAnimationFrame === 'function') root.cancelAnimationFrame(loopRaf);
      loopRaf = 0;
      canvas.style.cursor = savedCursor || '';
      current = null;
      selection = null;
      drag = null;
    }

    var instance = {
      enable: enable,
      destroy: destroy,
      save: save,
      reset: reset,
      cancel: cancel,
      getSelection: function () {
        return selection ? { kind: selection.kind, id: selection.id } : null;
      },
      getLayout: function () { return current ? clone(current) : null; },
      getError: function () { return lastError; },
      isEnabled: function () { return enabled; }
    };

    if (opts.enabled) enable(true);
    return instance;
  }

  var api = { attach: attach };
  root.AOLayoutEditor = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
