(function () {
  'use strict';

  const HOME = { azimuth: -29, pitch: 42, zoom: 1, x: 0, y: 0 };
  const COLORS = {
    running: '#5bf2ff', completed: '#55dfb1', failed: '#ff748b', interrupted: '#ff748b',
    waiting: '#d6ac66', retrying: '#ffd080'
  };
  const RAD = Math.PI / 180;
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const ATTENTION = new Set(['failed', 'interrupted', 'retrying']);
  const LIFT = { gateway: 27, stack: 43, module: 33 };

  function createProjection(world, viewport, camera, margin) {
    const sine = Math.sin(camera.azimuth * RAD);
    const cosine = Math.cos(camera.azimuth * RAD);
    const tilt = Math.cos(camera.pitch * RAD);
    const elevation = Math.sin(camera.pitch * RAD);
    const distance = Math.hypot(world.width, world.height, world.top - world.bottom) * 1.46;
    const centerZ = (world.top + world.bottom) / 2;
    const eye = {
      x: world.width / 2 + distance * tilt * sine,
      y: world.height / 2 + distance * tilt * cosine,
      z: centerZ + distance * elevation
    };

    function cameraPoint(x, y, z) {
      const dx = x - world.width / 2;
      const dy = y - world.height / 2;
      const dz = z - centerZ;
      const forward = dx * sine + dy * cosine;
      const depth = forward * tilt + dz * elevation;
      const perspective = distance / (distance - depth);
      return {
        x: (dx * cosine - dy * sine) * perspective,
        y: (forward * elevation - dz * tilt) * perspective,
        depth
      };
    }

    const corners = [];
    for (const x of [0, world.width]) {
      for (const y of [0, world.height]) {
        for (const z of [world.bottom, world.top]) corners.push(cameraPoint(x, y, z));
      }
    }
    const xs = corners.map(point => point.x);
    const ys = corners.map(point => point.y);
    const left = Math.min(...xs), right = Math.max(...xs);
    const top = Math.min(...ys), bottom = Math.max(...ys);
    const scale = camera.zoom * Math.min(1.65,
      Math.max(viewport.width - 2 * margin, 1) / (right - left),
      Math.max(viewport.height - 2 * margin, 1) / (bottom - top));
    const centerX = viewport.width / 2 + camera.x - (left + right) / 2 * scale;
    const centerY = viewport.height / 2 + camera.y - (top + bottom) / 2 * scale;
    const project = (x, y, z = 0) => {
      const point = cameraPoint(x, y, z);
      return { x: centerX + point.x * scale, y: centerY + point.y * scale, depth: point.depth };
    };
    return { project, eye, scale };
  }

  function layoutNodes(ids, layout = [], align) {
    const remaining = new Set(ids);
    const positions = new Map();
    const columns = layout.reduce((widest, row) => Math.max(widest, row.length), 1);
    const center = Math.ceil(columns / 2);
    let rowNumber = 0;
    for (const row of layout) {
      // A leading null keeps a column open. Empty or stale rows take no space.
      const known = row.map((id, index) => ({ id, col: index + 1 })).filter(({ id }) => remaining.has(id));
      if (!known.length) continue;
      rowNumber++;
      for (const { id, col } of known) {
        if (remaining.delete(id)) positions.set(id, [rowNumber, row.length === 1 ? align || center : col]);
      }
    }
    for (const id of remaining) positions.set(id, [++rowNumber, center]);
    return { positions, columns, rows: rowNumber };
  }

  function measurePath(points) {
    const segments = [];
    let length = 0;
    for (let i = 1; i < points.length; i++) {
      const [x, y] = points[i - 1];
      const dx = points[i][0] - x;
      const dy = points[i][1] - y;
      const span = Math.hypot(dx, dy);
      if (span > 0.001) {
        segments.push({ x, y, dx, dy, span, start: length });
        length += span;
      }
    }
    return { segments, length };
  }

  function pointAt(path, distance) {
    const offset = clamp(distance, 0, path.length);
    const segment = path.segments.find(part => offset <= part.start + part.span) || path.segments.at(-1);
    const progress = (offset - segment.start) / segment.span;
    return [segment.x + segment.dx * progress, segment.y + segment.dy * progress];
  }

  function route(edge, boxes, width, pad) {
    const a = boxes.get(edge.source);
    const b = boxes.get(edge.target);
    if (!a || !b) return [];
    if (edge.source === edge.target) {
      const start = a.x + a.w < width - 2 * pad ? a.x + a.w : a.x;
      const side = start === a.x ? start - pad : start + pad;
      const lower = a.y + a.h * .76;
      const upper = a.y + a.h * .24;
      return [[start, lower], [side, lower], [side, upper], [start, upper]];
    }
    if (b.y < a.y - 5) {
      const left = b.x <= a.x;
      const aSide = left ? a.x : a.x + a.w;
      const bSide = left ? b.x : b.x + b.w;
      const side = (left ? Math.min(aSide, bSide) : Math.max(aSide, bSide)) + (left ? -pad : pad);
      const start = a.y + a.h * .68;
      const end = b.y + b.h * .32;
      return [[aSide, start], [side, start], [side, end], [bSide, end]];
    }
    if (Math.abs(a.y - b.y) < 5) {
      const right = b.x > a.x;
      const start = right ? a.x + a.w : a.x;
      const end = right ? b.x : b.x + b.w;
      const middle = a.y + a.h / 2;
      const clear = [...boxes.values()].every(box => box.y !== a.y || box === a || box === b ||
        box.x >= Math.max(start, end) || box.x + box.w <= Math.min(start, end));
      if (clear && Math.abs(a.h - b.h) < 5) {
        const finishY = b.y + b.h / 2;
        return [[start, middle], [(start + end) / 2, (middle + finishY) / 2], [end, finishY]];
      }
      const side = start + (right ? pad : -pad);
      const bottom = Math.max(a.y + a.h, b.y + b.h) + pad;
      return [[start, middle], [side, middle], [side, bottom], [b.x + b.w / 2, bottom], [b.x + b.w / 2, b.y + b.h]];
    }
    const start = [a.x + a.w / 2, a.y + a.h];
    const end = [b.x + b.w / 2, b.y];
    if (b.y - a.y - a.h > 60) {
      const side = start[0] < width / 2 ? a.x - pad : a.x + a.w + pad;
      return [start, [start[0], start[1] + pad], [side, start[1] + pad],
        [side, end[1] - pad], [end[0], end[1] - pad], end];
    }
    const middle = (start[1] + end[1]) / 2;
    return [start, [start[0], middle], [end[0], middle], end];
  }

  function elevatePath(points, project, level) {
    const count = points.length - 1;
    return points.map(([x, y], index) => screenPoint(project, x, y,
      index === 0 || index === count ? level : level + 8));
  }

  function drawArrow(ctx, path, color, size) {
    const [x, y] = pointAt(path, path.length);
    const [px, py] = pointAt(path, path.length - size);
    const angle = Math.atan2(y - py, x - px);
    polygon(ctx, [[x, y], [x - size * Math.cos(angle - .52), y - size * Math.sin(angle - .52)],
      [x - size * Math.cos(angle + .52), y - size * Math.sin(angle + .52)]], color);
  }

  function drawBeam(ctx, path, head, glow) {
    const tail = Math.max(0, head - 38);
    const front = Math.min(head, path.length);
    if (front > tail) {
      const start = pointAt(path, tail);
      const end = pointAt(path, front);
      const color = gradient(ctx, start, end, '#20bcff06', '#a8f7ff');
      ctx.beginPath();
      ctx.moveTo(...start);
      for (const segment of path.segments) {
        if (segment.start > tail && segment.start < front) ctx.lineTo(segment.x, segment.y);
      }
      ctx.lineTo(...end);
      ctx.strokeStyle = '#3db8ff47';
      ctx.lineWidth = 6;
      ctx.stroke();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();
      if (head <= path.length) ctx.drawImage(glow, end[0] - 12, end[1] - 12, 24, 24);
    }
    for (const offset of [14, 26, 45]) {
      const distance = head - offset;
      if (distance <= 0 || distance >= path.length) continue;
      const [x, y] = pointAt(path, distance);
      ctx.fillStyle = offset === 14 ? '#b9faff' : '#66c8ffb3';
      ctx.beginPath();
      ctx.arc(x, y, offset === 14 ? 1.55 : 1, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function beamGlow() {
    const sprite = document.createElement('canvas');
    sprite.width = sprite.height = 32;
    const ctx = sprite.getContext('2d');
    if (!ctx) return sprite;
    const halo = ctx.createRadialGradient(16, 16, 0, 16, 16, 16);
    halo.addColorStop(0, '#efffff');
    halo.addColorStop(.16, '#91f9ff');
    halo.addColorStop(.36, '#44ddff91');
    halo.addColorStop(1, '#127eff00');
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, 32, 32);
    return sprite;
  }

  function polygon(ctx, points, fill, stroke) {
    ctx.beginPath();
    points.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.stroke();
    }
  }

  function screenPoint(project, x, y, z = 0) {
    const point = project(x, y, z);
    return [point.x, point.y];
  }

  function gradient(ctx, from, to, first, last) {
    const fill = ctx.createLinearGradient(...from, ...to);
    fill.addColorStop(0, first);
    fill.addColorStop(1, last);
    return fill;
  }

  function slab(ctx, { project, eye }, box, bottom, top, inset, surface, outline) {
    const { x, y, w, h } = box;
    const corners = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
    const upper = corners.map(([px, py]) => screenPoint(project,
      px < x + w / 2 ? px + inset : px - inset, py < y + h / 2 ? py + inset : py - inset, top));
    const lower = corners.map(([px, py]) => screenPoint(project, px, py, bottom));
    const sides = [
      eye.y < y + inset && [0, 1],
      eye.x > x + w - inset && [1, 2],
      eye.y > y + h - inset && [2, 3],
      eye.x < x + inset && [3, 0]
    ];
    sides.forEach((edge, index) => {
      if (!edge) return;
      const [a, b] = edge;
      const dark = index % 2 ? '#0d2037' : '#102137';
      const light = index % 2 ? '#2b6682' : '#23506e';
      polygon(ctx, [upper[a], upper[b], lower[b], lower[a]], gradient(ctx, upper[a], lower[a], light, dark));
    });
    const fill = gradient(ctx, upper[0], upper[2], surface, '#141f33');
    polygon(ctx, upper, fill, outline);
  }

  function drawBoard(ctx, projection, world) {
    const { width, height } = world;
    const p = (x, y, z) => screenPoint(projection.project, x, y, z);
    ctx.lineWidth = 1;
    slab(ctx, projection, { x: 0, y: 0, w: width, h: height }, world.bottom, 0, 0, '#162d47', '#416481');

    const grid = Math.max(34, Math.max(width, height) / 64);
    ctx.beginPath();
    for (let x = grid; x < width; x += grid) {
      ctx.moveTo(...p(x, 0));
      ctx.lineTo(...p(x, height));
    }
    for (let y = grid; y < height; y += grid) {
      ctx.moveTo(...p(0, y));
      ctx.lineTo(...p(width, y));
    }
    ctx.strokeStyle = '#5ab5d923';
    ctx.stroke();

    ctx.beginPath();
    for (const x of [0, width]) {
      for (const y of [0, height]) {
        ctx.moveTo(...p(x === 0 ? 42 : width - 42, y));
        ctx.lineTo(...p(x, y));
        ctx.lineTo(...p(x, y === 0 ? 42 : height - 42));
      }
    }
    ctx.strokeStyle = '#68d7f1ad';
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.beginPath();
    for (const y of [12, height - 12]) {
      ctx.moveTo(...p(12, y, -8));
      ctx.lineTo(...p(width - 12, y, -8));
    }
    ctx.strokeStyle = '#527eae60';
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  function drawDevice(ctx, projection, box, state) {
    const lit = COLORS[state];
    const accent = lit || '#4d7899';
    const { x, y, w, h, lift, shape } = box;
    const p = (px, py, z) => screenPoint(projection.project, px, py, z);
    const centerX = x + w / 2;
    const centerY = y + h / 2;

    ctx.lineWidth = 1;
    slab(ctx, projection, box, 0, 8, 0, '#193b54', '#4a6c87');
    const core = { x: x + 12, y: y + 9, w: w - 24, h: h - 18 };
    if (shape === 'stack') {
      slab(ctx, projection, core, 8, lift * .5, 0, '#214968', '#496a8d');
    }
    slab(ctx, projection, core, shape === 'stack' ? lift * .5 + 2 : 8, lift, 4,
      lit ? '#2b617b' : '#2b4361', accent);
    if (shape === 'gateway') {
      const ring = [[centerX, centerY - 12], [centerX + 21, centerY], [centerX, centerY + 12], [centerX - 21, centerY]];
      polygon(ctx, ring.map(([px, py]) => p(px, py, lift + 1)), '#0d233c', accent);
    } else if (projection.scale > .38) {
      ctx.beginPath();
      for (const offset of [-10, 0, 10]) {
        ctx.moveTo(...p(centerX + offset, centerY + 6, lift + 1));
        ctx.lineTo(...p(centerX + offset, centerY + 13, lift + 1));
      }
      ctx.strokeStyle = accent;
      ctx.stroke();
    }

    const indicator = p(centerX, centerY - 4, lift + 1);
    if (lit === COLORS.running || lit === COLORS.failed) {
      const halo = ctx.createRadialGradient(...indicator, 0, ...indicator, 13);
      halo.addColorStop(0, accent + 'b0');
      halo.addColorStop(1, accent + '00');
      ctx.fillStyle = halo;
      ctx.fillRect(indicator[0] - 13, indicator[1] - 13, 26, 26);
    }
    ctx.fillStyle = accent;
    ctx.beginPath();
    ctx.arc(...indicator, lit ? 3.6 : 2.2, 0, 2 * Math.PI);
    ctx.fill();
    if (state === 'running') {
      const end = p(centerX, centerY - 4, lift + 26);
      ctx.beginPath();
      ctx.moveTo(...indicator);
      ctx.lineTo(...end);
      ctx.lineWidth = 4;
      ctx.strokeStyle = gradient(ctx, indicator, end, '#92f9ffa0', '#34bdff00');
      ctx.stroke();
    }
  }

  function prepareCanvas(canvas, ctx, width, height, maxRatio) {
    // Keep the backing store bounded on high-DPI displays and long flat diagrams.
    const ratio = Math.min(window.devicePixelRatio || 1, maxRatio, Math.sqrt(2500000 / (width * height)));
    const pixelWidth = Math.max(1, Math.round(width * ratio));
    const pixelHeight = Math.max(1, Math.round(height * ratio));
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    ctx.setTransform(pixelWidth / width, 0, 0, pixelHeight / height, 0, 0);
    ctx.clearRect(0, 0, width, height);
  }

  function createScene(paintStatus, config = {}) {
    const diagram = document.getElementById('workflowDiagram');
    const nodes = document.getElementById('workflowNodes');
    const links = document.getElementById('workflowEdgeList');
    const canvas = document.getElementById('workflowEdges');
    const flow = document.getElementById('workflowFlow');
    const ctx = canvas.getContext('2d');
    const flowCtx = flow.getContext('2d');
    const glow = flowCtx ? beamGlow() : null;
    const controls = document.getElementById('workflowControls');
    const cameraButtons = document.getElementById('workflowCamera');
    const zoomLabel = document.getElementById('workflowZoom');
    const hint = document.getElementById('workflowCameraHint');
    const smallScreen = matchMedia('(max-width: 899px)');
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    const touchInput = matchMedia('(pointer: coarse)');
    const elements = new Map();
    const home = { ...HOME, ...config.camera };
    const flowSpeed = Number.isFinite(config.flowSpeed) ? Math.max(0, config.flowSpeed) : 125;
    let graph, profile, mode, preferredMode, geometry, layout, labelSizes, canvasRatio;
    const camera = { ...home };
    let onscreen = false;
    let drawFrame = 0;
    let flowFrame = 0;
    let lastFlow = 0;
    let viewport = { width: 0, height: 0 };
    let movingPaths = [];

    function positionNodes() {
      layout = layoutNodes([...elements.keys()], profile[mode], mode === 'flat' ? profile.align : undefined);
      labelSizes = null;
      nodes.style.setProperty('--columns', layout.columns);
      for (const [id, [row, column]] of layout.positions) {
        const { element } = elements.get(id);
        element.style.gridRow = row;
        element.style.gridColumn = column;
      }
      geometry = null;
      redraw();
    }

    function setGraph(next, nextView) {
      graph = next;
      profile = config.views?.[nextView] || {};
      elements.clear();
      nodes.replaceChildren();
      links.replaceChildren();
      diagram.hidden = controls.hidden = !graph;
      if (!graph) {
        geometry = null;
        movingPaths = [];
        diagram.removeAttribute('data-flowing');
        syncMotion();
        return;
      }
      const data = new Map(graph.nodes.map(node => [node.id, { ...node, ...config.nodes?.[node.id] }]));
      for (const id of layoutNodes([...data.keys()], profile.flat, profile.align).positions.keys()) {
        const { label = id, shape = 'module', height } = data.get(id);
        const element = App.create('div', 'workflow-node');
        element.dataset.node = id;
        const status = App.create('span', 'workflow-status');
        const visits = App.create('span', 'workflow-visits');
        visits.hidden = true;
        element.append(App.create('strong', '', label), status, visits);
        nodes.append(element);
        const lift = Number.isFinite(height) ? clamp(height, 20, 78) : LIFT[shape] ?? LIFT.module;
        elements.set(id, { element, status, visits, label, shape, lift });
      }
      for (const edge of graph.edges) {
        const source = elements.get(edge.source);
        const target = elements.get(edge.target);
        if (source && target) links.append(App.create('li', '', source.label + ' → ' + target.label));
      }
      resetCamera();
      setMode(preferredMode || (smallScreen.matches ? 'flat' : 'space'));
      syncMotion();
    }

    function setRun(run) {
      let changed = false;
      let running = false;
      for (const [id, { element, status, visits, label }] of elements) {
        const node = run?.nodes?.[id];
        const state = node?.status || (run?.legacy ? 'unknown' : 'idle');
        running ||= state === 'running';
        if (element.dataset.state !== state) {
          element.dataset.state = state;
          element.title = label + ' · ' + paintStatus(status, state);
          changed = true;
        }
        const count = node?.visits || 0;
        const countLabel = count > 1 ? '×' + count : '';
        if (visits.textContent !== countLabel) {
          visits.hidden = count <= 1;
          visits.textContent = countLabel;
          visits.setAttribute('aria-label', '执行 ' + count + ' 次');
        }
      }
      diagram.toggleAttribute('data-flowing', running);
      if (changed) {
        labelSizes = null;
        redraw();
      }
    }

    function buildGeometry() {
      const boxes = new Map();
      if (mode === 'space') {
        const w = 110, h = 58;
        const [columnGap, rowGap] = profile.spacing || [211, 124];
        const border = 55;
        let top = 70;
        for (const [id, [row, column]] of layout.positions) {
          const { lift, shape } = elements.get(id);
          boxes.set(id, { x: border + (column - 1) * columnGap, y: border + (row - 1) * rowGap, w, h, lift, shape });
          top = Math.max(top, lift + 26);
        }
        const world = { width: 2 * border + w + (layout.columns - 1) * columnGap,
          height: 2 * border + h + (Math.max(1, layout.rows) - 1) * rowGap, bottom: -25, top };
        return { boxes, world, paths: graph.edges.map(edge => route(edge, boxes, world.width, 16)) };
      }

      const bounds = diagram.getBoundingClientRect();
      for (const [id, { element }] of elements) {
        const rect = element.getBoundingClientRect();
        boxes.set(id, { x: rect.left - bounds.left - diagram.clientLeft,
          y: rect.top - bounds.top - diagram.clientTop, w: rect.width, h: rect.height });
      }
      return { boxes, paths: graph.edges.map(edge => route(edge, boxes, viewport.width, viewport.width < 460 ? 5 : 9)) };
    }

    function placeLabels(ctx, devices, viewport) {
      if (!labelSizes) {
        labelSizes = new Map([...elements].map(([id, { element }]) =>
          [id, { width: element.offsetWidth, height: element.offsetHeight }]));
      }
      const placed = [];
      const priority = id => {
        const state = elements.get(id).element.dataset.state;
        return Number(state === 'running' || ATTENTION.has(state));
      };
      const frontFirst = devices.slice().reverse();
      frontFirst.sort((a, b) => priority(b.id) - priority(a.id));
      ctx.beginPath();
      for (const { id, anchor } of frontFirst) {
        const { element } = elements.get(id);
        const { width, height } = labelSizes.get(id);
        if (anchor.x < -12 || anchor.x > viewport.width + 12 || anchor.y < -12 || anchor.y > viewport.height + 12) {
          element.toggleAttribute('data-outside', true);
          continue;
        }
        element.removeAttribute('data-outside');
        const centerX = clamp(anchor.x, width / 2 + 4, viewport.width - width / 2 - 4);
        const gap = 5;
        // Prefer labels directly above the device. Spill into the side gutters when they overlap.
        const offsets = [[0, -height - 10], [0, 11], [-width / 2 - 22, -height / 2],
          [width / 2 + 22, -height / 2], [0, -height * 2 - 14], [0, height + 14],
          [-width - 12, -height - 10], [width + 12, -height - 10],
          [-width - 12, 11], [width + 12, 11]];
        const candidates = offsets.map(([dx, dy]) => ({
          x: clamp(centerX + dx - width / 2, 4, viewport.width - width - 4),
          y: clamp(anchor.y + dy, 4, viewport.height - height - 4), width, height
        }));
        const fits = candidate => placed.every(other =>
          candidate.x + width + gap <= other.x || other.x + other.width + gap <= candidate.x ||
          candidate.y + height + gap <= other.y || other.y + other.height + gap <= candidate.y);
        let label = candidates.find(fits);
        if (!label) {
          // Search the nearby rows before using a distant gutter for this label.
          for (let offset = height + 24; offset < viewport.height && !label; offset += height + gap) {
            for (const y of [anchor.y - offset - height, anchor.y + offset]) {
              if (y < 4 || y + height > viewport.height - 4) continue;
              label = candidates.map(candidate => ({ ...candidate, y })).find(fits);
              if (label) break;
            }
          }
        }
        label ||= candidates[0];
        placed.push(label);
        element.style.setProperty('--label-x', label.x.toFixed(1) + 'px');
        element.style.setProperty('--label-y', label.y.toFixed(1) + 'px');
        const x = clamp(anchor.x, label.x, label.x + width);
        const y = clamp(anchor.y, label.y, label.y + height);
        if (Math.hypot(x - anchor.x, y - anchor.y) > 3) {
          ctx.moveTo(anchor.x, anchor.y);
          ctx.lineTo(x, y);
        }
      }
      ctx.strokeStyle = '#6daacb91';
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    function draw() {
      drawFrame = 0;
      if (!graph || !ctx || !onscreen || document.hidden) return;
      const width = diagram.clientWidth;
      const height = diagram.clientHeight;
      if (!width || !height) return;
      if (viewport.width !== width || viewport.height !== height) geometry = null;
      viewport = { width, height };
      canvasRatio = window.devicePixelRatio;
      geometry ||= buildGeometry();
      prepareCanvas(canvas, ctx, width, height, 2);

      const { boxes, world, paths } = geometry;
      let projection = null;
      if (world) {
        const margin = width < 500 ? 18 : 25;
        projection = createProjection(world, viewport, camera, margin);
        const nextHint = touchInput.matches ? '拖动旋转 · 双指缩放和平移' :
          '拖动旋转 · Shift 拖动平移 · 滚轮缩放';
        if (hint.textContent !== nextHint) hint.textContent = nextHint;
        drawBoard(ctx, projection, world);
      }

      movingPaths = [];
      graph.edges.forEach((edge, index) => {
        const points = projection ? elevatePath(paths[index], projection.project, 9) : paths[index];
        const path = measurePath(points);
        if (!path.length) return;
        const from = elements.get(edge.source).element.dataset.state;
        const to = elements.get(edge.target).element.dataset.state;
        const active = to === 'running' && (from === 'running' || from === 'completed');
        const attention = ATTENTION.has(to) && (from === 'running' || from === 'completed');
        ctx.beginPath();
        ctx.moveTo(...points[0]);
        for (let i = 1; i < points.length; i++) ctx.lineTo(...points[i]);
        if (active) {
          ctx.setLineDash([]);
          ctx.strokeStyle = '#3fa6ff39';
          ctx.lineWidth = 7;
          ctx.stroke();
          movingPaths.push(path);
        }
        const ink = active ? '#70dbff' : attention ? COLORS[to] : '#557998';
        ctx.strokeStyle = ink;
        ctx.lineWidth = active || attention ? 2 : 1.15;
        ctx.setLineDash(edge.conditional && !active && !attention ? [4, 4] : []);
        ctx.stroke();
        drawArrow(ctx, path, ink, active ? 7 : 5);
      });
      ctx.setLineDash([]);

      if (projection) {
        const ordered = [...boxes].map(([id, box]) => ({
          id, box, anchor: projection.project(box.x + box.w / 2, box.y + box.h / 2, box.lift)
        })).sort((a, b) => a.anchor.depth - b.anchor.depth);
        for (const { id, box } of ordered) {
          drawDevice(ctx, projection, box, elements.get(id).element.dataset.state);
        }
        placeLabels(ctx, ordered, viewport);
      }
      if (flowCtx) prepareCanvas(flow, flowCtx, width, height, 1.5);
      syncMotion();
    }

    function redraw() {
      if (!drawFrame) drawFrame = requestAnimationFrame(draw);
    }

    function animateFlow(now) {
      flowFrame = requestAnimationFrame(animateFlow);
      if (now - lastFlow < 32) return;
      lastFlow = now;
      flowCtx.clearRect(0, 0, viewport.width, viewport.height);
      flowCtx.lineCap = flowCtx.lineJoin = 'round';
      movingPaths.forEach((path, index) => {
        const head = (now * flowSpeed / 1000 + index * 53) % (path.length + 46);
        drawBeam(flowCtx, path, head, glow);
      });
    }

    function syncMotion() {
      const motionEnabled = Boolean(graph && onscreen && !document.hidden && !reducedMotion.matches);
      diagram.toggleAttribute('data-motion', motionEnabled);
      if (motionEnabled && flowCtx && flowSpeed > 0 && movingPaths.length) {
        if (!flowFrame) flowFrame = requestAnimationFrame(animateFlow);
      } else if (flowFrame) {
        cancelAnimationFrame(flowFrame);
        flowFrame = 0;
        flowCtx.clearRect(0, 0, viewport.width, viewport.height);
      }
    }

    function setMode(next) {
      if (!ctx) next = 'flat';
      mode = next;
      diagram.dataset.mode = next;
      const isSpace = next === 'space';
      cameraButtons.hidden = hint.hidden = !isSpace;
      diagram.tabIndex = isSpace ? 0 : -1;
      if (isSpace) diagram.setAttribute('aria-describedby', hint.id);
      else diagram.removeAttribute('aria-describedby');
      for (const button of controls.querySelectorAll('[data-mode]')) {
        button.setAttribute('aria-pressed', String(button.dataset.mode === next));
        button.disabled = !ctx && button.dataset.mode === 'space';
      }
      if (graph) positionNodes();
    }

    function adjustCamera(change) {
      Object.assign(camera, change);
      zoomLabel.textContent = Math.round(camera.zoom * 100) + '%';
      redraw();
    }

    function resetCamera() {
      adjustCamera(home);
    }

    function zoom(factor, focusX = 0, focusY = 0) {
      const level = clamp(camera.zoom * factor, .65, 3.6);
      const ratio = level / camera.zoom;
      adjustCamera({ zoom: level, x: focusX + (camera.x - focusX) * ratio,
        y: focusY + (camera.y - focusY) * ratio });
    }

    function rotate(azimuth, pitch = 0) {
      adjustCamera({ azimuth: (camera.azimuth + azimuth) % 360, pitch: clamp(camera.pitch + pitch, 28, 68) });
    }

    const actions = {
      left: () => rotate(-18),
      right: () => rotate(18),
      in: () => zoom(1.23),
      out: () => zoom(1 / 1.23),
      reset: resetCamera
    };
    controls.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button) return;
      if (button.dataset.mode) {
        preferredMode = button.dataset.mode;
        setMode(preferredMode);
      } else {
        actions[button.dataset.camera]?.();
      }
    });

    diagram.addEventListener('wheel', event => {
      if (mode !== 'space' || event.ctrlKey || event.metaKey) return;
      event.preventDefault();
      const bounds = diagram.getBoundingClientRect();
      const delta = event.deltaY * (event.deltaMode === 1 ? 15 : 1);
      zoom(Math.exp(-clamp(delta, -200, 200) * .002),
        event.clientX - bounds.left - bounds.width / 2, event.clientY - bounds.top - bounds.height / 2);
    }, { passive: false });

    diagram.addEventListener('keydown', event => {
      if (mode !== 'space') return;
      const keys = { ArrowLeft: actions.left, ArrowRight: actions.right, ArrowUp: () => rotate(0, 5),
        ArrowDown: () => rotate(0, -5), '+': actions.in, '=': actions.in, '-': actions.out, Home: actions.reset };
      if (!Object.hasOwn(keys, event.key)) return;
      event.preventDefault();
      keys[event.key]();
    });

    const pointers = new Map();
    diagram.addEventListener('pointerdown', event => {
      if (mode !== 'space' || event.button > 1 || pointers.size >= 2) return;
      if (event.button === 1) event.preventDefault();
      diagram.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      diagram.toggleAttribute('data-dragging', true);
    });
    diagram.addEventListener('pointermove', event => {
      const before = pointers.get(event.pointerId);
      if (!before || mode !== 'space') return;
      const after = { x: event.clientX, y: event.clientY };
      pointers.set(event.pointerId, after);
      const dx = after.x - before.x;
      const dy = after.y - before.y;
      if (pointers.size === 2) {
        const other = [...pointers.entries()].find(([id]) => id !== event.pointerId)[1];
        const distance = Math.hypot(before.x - other.x, before.y - other.y);
        const current = Math.hypot(after.x - other.x, after.y - other.y);
        const bounds = diagram.getBoundingClientRect();
        adjustCamera({ x: camera.x + dx / 2, y: camera.y + dy / 2 });
        if (distance > 0) zoom(current / distance, (after.x + other.x - bounds.width) / 2 - bounds.left,
          (after.y + other.y - bounds.height) / 2 - bounds.top);
      } else if (event.shiftKey || event.buttons === 4) {
        adjustCamera({ x: camera.x + dx, y: camera.y + dy });
      } else {
        rotate(dx * .45, dy * -.25);
      }
    });
    function endPointer(event) {
      pointers.delete(event.pointerId);
      diagram.toggleAttribute('data-dragging', pointers.size > 0);
    }
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      diagram.addEventListener(type, endPointer);
    }

    smallScreen.addEventListener('change', () => {
      if (!preferredMode) setMode(smallScreen.matches ? 'flat' : 'space');
    });
    reducedMotion.addEventListener('change', syncMotion);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) redraw();
      syncMotion();
    });
    new IntersectionObserver(([entry]) => {
      onscreen = entry.isIntersecting;
      if (onscreen) redraw();
      else syncMotion();
    }).observe(diagram);
    const resize = new ResizeObserver(() => {
      // Flat geometry uses the measured node sizes, which can change as text wraps.
      geometry = null;
      labelSizes = null;
      redraw();
    });
    resize.observe(diagram);
    resize.observe(nodes);
    window.addEventListener('resize', () => {
      if (canvasRatio !== window.devicePixelRatio) redraw();
    });

    return { setGraph, setRun };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { createProjection, layoutNodes, measurePath, pointAt };
  else window.createWorkflowScene = createScene;
})();
