// Logic for "Episteme 3D.dc.html" — the 3D truth model of the Episteme mark.
// Extracted from the inline <script data-dc-script> block so it can be edited,
// linted and diffed as plain JS. The .dc.html loads this file in <head>; its
// script block is now a one-line shim that calls the factory below.
//
// dc-runtime evaluates the .dc.html script body with `DCLogic` injected as a
// function PARAMETER, not as a global — so the class cannot be declared here
// directly; it is produced by a factory that takes the base class.
//
// See README.md in this folder for the design doc and the known-issue list.

window.EpistemeObeliskWeave = function (DCLogic) {
  // 3D truth model. World: y up, plane at y=0, camera on +z looking at the origin.
  // The obelisk is a real convex octahedron; the weave is a real grid on the plane.
  // Visibility comes from ray/solid tests, not from draw order — so this can be
  // frozen and emitted as SVG paths without changing a line of the geometry.
  class Component extends DCLogic {
    componentDidMount() {
      this.start = performance.now();
      this.t = 0;
      this.ripples = [];
      this.nextSpawn = 0.3;
      const loop = (now) => {
        this.t = (now - this.start) / 1000;
        this.pump();
        this.draw();
        this._raf = requestAnimationFrame(loop);
      };
      this._raf = requestAnimationFrame(loop);
    }
    componentWillUnmount() { cancelAnimationFrame(this._raf); }

    renderVals() { return { setCanvas: (el) => { this.canvas = el; } }; }

    cfg() {
      const p = this.props || {};
      return {
        cell: p.cell ?? 0.95,
        gridPhase: p.gridPhase ?? 0.5,
        obeliskY: p.obeliskY ?? 0,
        elong: p.elong ?? 2.4,
        girth: p.girth ?? 0.42,
        camY: p.camY ?? 0.85,
        camDist: p.camDist ?? 4.6,
        fov: p.fov ?? 34,
        gravity: p.gravity ?? 0.55,
        wellR: p.wellR ?? 2.2,
        exclusion: p.exclusion ?? 0.1,
        massVar: p.massVar ?? 0.3,
        chargeVar: p.chargeVar ?? 0.3,
        rippleAmp: p.rippleAmp ?? 0.16,
        rippleRate: p.rippleRate ?? 1,
        rippleSpeed: p.rippleSpeed ?? 1.5,
        rippleFade: p.rippleFade ?? 1.5,
        weave: p.weaveColor ?? '#8DA6C0',
        albedo: p.albedo ?? '#9AA2AC',
        roughness: p.roughness ?? 0.75,
        sheen: p.sheen ?? 0.06,
        lightAz: p.lightAz ?? 0.9,
        edge: p.edge ?? 0.2,
      };
    }

    pickOrigin() {
      const recent = this.ripples.slice(-7);
      let best = null, bestD = -1;
      for (let i = 0; i < 14; i++) {
        const x = Math.random() * 16 - 8;
        const z = Math.random() * 18 - 14;
        let d = Infinity;
        for (const r of recent) d = Math.min(d, Math.hypot(x - r.x, z - r.z));
        if (!recent.length) d = Math.random();
        if (d > bestD) { bestD = d; best = { x, z }; }
      }
      return best;
    }

    pump() {
      const k = this.cfg();
      if (k.rippleRate > 0 && this.t > this.nextSpawn) {
        const o = this.pickOrigin();
        this.ripples.push({ x: o.x, z: o.z, t0: this.t, amp: 0.65 + Math.random() * 0.7, life: 5.5 + Math.random() * 3.5 });
        this.nextSpawn = this.t + (0.5 + Math.random() * 1.2) / k.rippleRate;
      }
      this.ripples = this.ripples.filter(r => this.t - r.t0 < r.life);
    }

    // vertical displacement of the fabric at a point on the plane
    field(x, z, k) {
      let sum = 0;
      for (const R of this.ripples) {
        const age = this.t - R.t0;
        if (age < 0) continue;
        const r = Math.hypot(x - R.x, z - R.z);
        const crest = k.rippleSpeed * age;
        const sigma = 0.85 + 0.5 * age;
        const q = (r - crest) / sigma;
        if (q * q > 10) continue;
        // bell envelope over the ripple's whole life: it swells in and settles out
        // instead of appearing at full amplitude and decaying
        const bell = Math.pow(Math.sin(Math.PI * Math.min(1, age / R.life)), k.rippleFade);
        sum += R.amp * Math.exp(-q * q) * bell * (2.4 / (2.4 + crest)) * Math.cos(q * 1.7);
      }
      return sum;
    }

    rgb(hex) {
      let h = (hex || '').trim();
      if (h[0] === '#') h = h.slice(1);
      if (h.length === 3) h = h.split('').map(c => c + c).join('');
      const n = parseInt(h || '888888', 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    draw() {
      const c = this.canvas;
      if (!c) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = c.getBoundingClientRect();
      const w = rect.width, h = rect.height;
      if (!w || !h) return;
      if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
        c.width = Math.round(w * dpr);
        c.height = Math.round(h * dpr);
      }
      const ctx = c.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const k = this.cfg();
      const weave = this.rgb(k.weave);
      const alb = this.rgb(k.albedo);

      // --- solid: octahedron half-extents ---
      const Hy = k.elong * k.girth, Wx = k.girth, Wz = k.girth;
      const oy = k.obeliskY;              // vertical position of the solid in the world
      const TIP = [0, oy - Hy, 0];

      // --- camera ---
      const cam = [0, k.camY, k.camDist];
      const target = [0, 0.02, 0];
      const nrm = (v) => { const m = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / m, v[1] / m, v[2] / m]; };
      const crs = (p, q) => [p[1] * q[2] - p[2] * q[1], p[2] * q[0] - p[0] * q[2], p[0] * q[1] - p[1] * q[0]];
      const fwd = nrm([target[0] - cam[0], target[1] - cam[1], target[2] - cam[2]]);
      const rgt = nrm(crs(fwd, [0, 1, 0]));
      const upv = crs(rgt, fwd);
      const focal = (h * 0.5) / Math.tan((k.fov * Math.PI) / 360);
      const cxs = w / 2, cys = h * 0.5;
      const project = (p) => {
        const vx = p[0] - cam[0], vy = p[1] - cam[1], vz = p[2] - cam[2];
        const d = vx * fwd[0] + vy * fwd[1] + vz * fwd[2];
        if (d < 0.05) return null;
        const xc = vx * rgt[0] + vy * rgt[1] + vz * rgt[2];
        const yc = vx * upv[0] + vy * upv[1] + vz * upv[2];
        return { x: cxs + (xc / d) * focal, y: cys - (yc / d) * focal, d };
      };

      // --- 8 half-planes of the solid: n·p <= 1 ---
      const planes = [];
      for (const sx of [1, -1]) for (const sy of [1, -1]) for (const sz of [1, -1]) {
        planes.push([sx / Wx, sy / Hy, sz / Wz]);
      }
      const sdf = (p) => {
        const f = Math.abs(p[0]) / Wx + Math.abs(p[1] - oy) / Hy + Math.abs(p[2]) / Wz - 1;
        const g = Math.hypot(1 / Wx, 1 / Hy, 1 / Wz);
        return f / g;
      };
      // Cast from the CAMERA toward the point: tIn is how far the solid's near
      // surface is, len how far the point is. len < tIn means the point is in
      // front of the solid. Measuring from the camera makes the comparison a
      // straight depth test, so nothing has to be deleted to look right.
      const inFront = (p) => {
        const dx = p[0] - cam[0], dy = p[1] - cam[1], dz = p[2] - cam[2];
        const len = Math.hypot(dx, dy, dz);
        const D = [dx / len, dy / len, dz / len];
        const o = [cam[0], cam[1] - oy, cam[2]];
        let tIn = -Infinity, tOut = Infinity;
        for (const n of planes) {
          const a = n[0] * D[0] + n[1] * D[1] + n[2] * D[2];
          const b = 1 - (n[0] * o[0] + n[1] * o[1] + n[2] * o[2]);
          if (Math.abs(a) < 1e-9) { if (b < 0) return false; continue; }
          const t = b / a;
          if (a > 0) { if (t < tOut) tOut = t; } else { if (t > tIn) tIn = t; }
        }
        if (!(tIn <= tOut && tOut > 1e-4)) return false;   // ray misses the solid
        return len < tIn;
      };

      // --- a point of fabric, resolved under both forces ---
      // mass and charge are FIELDS over the plane, not per-strand constants: a row
      // and a column crossing the same spot must resolve to the same point, or the
      // lattice stops being one surface and the two families drift apart
      const vh = (i, j) => { const v = Math.sin(i * 127.1 + j * 311.7) * 43758.5453; return v - Math.floor(v); };
      const noise = (x, z, sc) => {
        const X = x / sc, Z = z / sc;
        const i = Math.floor(X), j = Math.floor(Z);
        const fx = X - i, fz = Z - j;
        const sx = fx * fx * (3 - 2 * fx), sz = fz * fz * (3 - 2 * fz);
        const a = vh(i, j), b = vh(i + 1, j), c2 = vh(i, j + 1), d2 = vh(i + 1, j + 1);
        return (a * (1 - sx) + b * sx) * (1 - sz) + (c2 * (1 - sx) + d2 * sx) * sz;
      };
      const fabric = (x, z) => {
        const base = project([x, 0, z]);
        if (!base) return null;
        const mass = 1 + k.massVar * (noise(x, z, 3.6) * 2 - 1);
        const charge = 1 + k.chargeVar * (noise(x + 41, z - 17, 2.9) * 2 - 1);
        let p = [x, 0, z];
        // GRAVITY: long range, attractive, toward the mass at the tip
        const dx = TIP[0] - p[0], dy = TIP[1] - p[1], dz = TIP[2] - p[2];
        const dist = Math.hypot(dx, dy, dz) || 1e-3;
        const mag = (k.gravity * mass) / (1 + Math.pow(dist / k.wellR, 2));
        p = [p[0] + (dx / dist) * mag, p[1] + (dy / dist) * mag, p[2] + (dz / dist) * mag];
        // ripple, vertical — a = F/m, so heavier sections answer a passing wave less
        const e = this.field(x, z, k) / mass;
        p[1] += e;
        // EXCLUSION: short range, repulsive — never inside the solid
        const margin = k.exclusion * charge;
        const d = sdf(p);
        if (d < margin) {
          const n = nrm([Math.sign(p[0] || 1) / Wx, Math.sign((p[1] - oy) || 1) / Hy, Math.sign(p[2] || 1) / Wz]);
          const push = margin - d;
          const soft = d < 0 ? 1 : Math.pow(1 - d / margin, 1.5);
          p = [p[0] + n[0] * push * soft, p[1] + n[1] * push * soft, p[2] + n[2] * push * soft];
        }
        // ORDER GUARANTEE, applied to the shared field so both families agree.
        // In-plane drift is capped at 0.45 cell, so nothing reorders laterally.
        // Vertical sag makes a height field — safe in world space by construction —
        // and is capped only by what perspective can invert at this depth.
        const lat = Math.hypot(p[0] - x, p[2] - z);
        const latLim = 0.45 * k.cell;
        if (lat > latLim) {
          const f = latLim / lat;
          p[0] = x + (p[0] - x) * f;
          p[2] = z + (p[2] - z) * f;
        }
        const vLim = (0.8 * k.camY * k.cell) / Math.max(1, base.d);
        p[1] = Math.max(-vLim, Math.min(vLim, p[1]));

        const s = project(p);
        if (!s) return null;
        return { x: s.x, y: s.y, d: s.d, e: Math.min(1, Math.abs(e) / 0.25), front: inFront(p) };
      };

      // --- facets, lit from real normals ---
      const el = 0.5;
      const Lv = nrm([Math.sin(k.lightAz) * Math.cos(el), Math.sin(el), Math.cos(k.lightAz) * Math.cos(el)]);
      const gloss = 2 + 140 * Math.pow(1 - k.roughness, 3);
      const faces = [];
      for (const sy of [1, -1]) for (const sx of [1, -1]) for (const sz of [1, -1]) {
        const A = [0, oy + sy * Hy, 0], Bv = [sx * Wx, oy, 0], C = [0, oy, sz * Wz];
        let n = nrm(crs([Bv[0] - A[0], Bv[1] - A[1], Bv[2] - A[2]], [C[0] - A[0], C[1] - A[1], C[2] - A[2]]));
        const cen = [(A[0] + Bv[0] + C[0]) / 3, (A[1] + Bv[1] + C[1]) / 3, (A[2] + Bv[2] + C[2]) / 3];
        const rc = [cen[0], cen[1] - oy, cen[2]];
        if (n[0] * rc[0] + n[1] * rc[1] + n[2] * rc[2] < 0) n = [-n[0], -n[1], -n[2]];
        const toCam = nrm([cam[0] - cen[0], cam[1] - cen[1], cam[2] - cen[2]]);
        if (n[0] * toCam[0] + n[1] * toCam[1] + n[2] * toCam[2] <= 0) continue;
        const nl = Math.max(0, n[0] * Lv[0] + n[1] * Lv[1] + n[2] * Lv[2]);
        const Hv = nrm([Lv[0] + toCam[0], Lv[1] + toCam[1], Lv[2] + toCam[2]]);
        const amb = 0.16 + 0.14 * k.roughness;
        const diff = amb + (1 - amb) * nl;
        const spec = k.sheen * Math.pow(Math.max(0, n[0] * Hv[0] + n[1] * Hv[1] + n[2] * Hv[2]), gloss);
        const cl = alb.map(ch => Math.max(0, Math.min(255, Math.round(ch * diff + 255 * spec))));
        faces.push({ tri: [A, Bv, C].map(project), fill: `rgb(${cl[0]},${cl[1]},${cl[2]})` });
      }
      const paintFaces = () => {
        ctx.lineJoin = 'round';
        for (const f of faces) {
          if (f.tri.some(p => !p)) continue;
          ctx.beginPath();
          ctx.moveTo(f.tri[0].x, f.tri[0].y);
          ctx.lineTo(f.tri[1].x, f.tri[1].y);
          ctx.lineTo(f.tri[2].x, f.tri[2].y);
          ctx.closePath();
          ctx.fillStyle = f.fill;
          ctx.fill();
          if (k.edge > 0) {
            const dk = alb.map(ch => Math.round(ch * 0.3));
            ctx.strokeStyle = `rgba(${dk[0]},${dk[1]},${dk[2]},${k.edge})`;
            ctx.lineWidth = 0.8;
            ctx.stroke();
          }
        }
      };

      // --- the weave: strands sampled densely, drawn in two depth passes ---
      const EX = 14, ZF = -16, ZN = k.camDist * 0.62;
      const fogA = 1.2, fogB = k.camDist + 12;
      ctx.lineCap = 'round';
      // One unbroken path per visible run. Depth fade and ripple energy are carried
      // by a gradient along the strand instead of by chopping it into pieces, so the
      // line stays continuous.
      const drawRun = (run) => {
        if (run.length < 2) return;
        let mBase = 0, mE = 0;
        for (const p of run) {
          mBase += Math.max(0, Math.min(1, (fogB - p.d) / (fogB - fogA)));
          mE += p.e;
        }
        mBase /= run.length;
        mE /= run.length;
        if (mBase <= 0.02) return;

        const A = run[0], Z = run[run.length - 1];
        const horiz = Math.abs(Z.x - A.x) >= Math.abs(Z.y - A.y);
        const span = horiz ? Z.x - A.x : Z.y - A.y;
        let paint;
        if (Math.abs(span) < 2) {
          paint = `rgba(${weave[0]},${weave[1]},${weave[2]},${Math.min(0.95, mBase * (0.6 + mE * 0.5))})`;
        } else {
          const g = horiz
            ? ctx.createLinearGradient(A.x, A.y, Z.x, A.y)
            : ctx.createLinearGradient(A.x, A.y, A.x, Z.y);
          const stride = Math.max(1, Math.ceil(run.length / 36));
          let last = -1;
          for (let i = 0; i < run.length; i += stride) {
            const p = run[i];
            let off = ((horiz ? p.x - A.x : p.y - A.y) / span);
            off = Math.max(0, Math.min(1, off));
            if (off <= last) continue;
            last = off;
            const fog = Math.max(0, Math.min(1, (fogB - p.d) / (fogB - fogA)));
            g.addColorStop(off, `rgba(${weave[0]},${weave[1]},${weave[2]},${Math.min(0.95, fog * (0.6 + p.e * 0.5))})`);
          }
          if (last < 1) {
            const p = Z;
            const fog = Math.max(0, Math.min(1, (fogB - p.d) / (fogB - fogA)));
            g.addColorStop(1, `rgba(${weave[0]},${weave[1]},${weave[2]},${Math.min(0.95, fog * (0.6 + p.e * 0.5))})`);
          }
          paint = g;
        }
        ctx.beginPath();
        run.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
        ctx.strokeStyle = paint;
        ctx.lineWidth = 0.5 + mBase * 1.3 + mE * 1.2;
        ctx.stroke();
      };
      // Every strand is drawn COMPLETE in the base layer — no classification can
      // ever remove a piece of one. The opaque body then covers whatever it stands
      // in front of, and only the stretches genuinely nearer than the solid are
      // painted again on top.
      const emit = (samples, frontOnly) => {
        let run = [];
        for (const s of samples) {
          if (s && (!frontOnly || s.front)) run.push(s);
          else { drawRun(run); run = []; }
        }
        drawRun(run);
      };

      // grid phase: at 0.5 the lattice straddles the origin, so the obelisk stands
      // in the middle of an open cell rather than being skewered by two strands
      const ph = k.gridPhase;
      const lines = [];
      const iMin = Math.ceil(ZF / k.cell - ph), iMax = Math.floor(ZN / k.cell - ph);
      for (let i = iMin; i <= iMax; i++) {
        const z = (i + ph) * k.cell;
        const n = 300;
        const samples = [];
        for (let q = 0; q <= n; q++) samples.push(fabric(-EX + (2 * EX * q) / n, z));
        lines.push(samples);
      }
      const nCols = Math.floor(EX / k.cell);
      for (let j = -nCols - 1; j <= nCols; j++) {
        const x = (j + ph) * k.cell;
        const a = project([x, 0, ZN]), b = project([x, 0, ZF]);
        if (a && b && ((a.x < -20 && b.x < -20) || (a.x > w + 20 && b.x > w + 20))) continue;
        const n = 240;
        const samples = [];
        for (let q = 0; q <= n; q++) samples.push(fabric(x, ZF + ((ZN - ZF) * q) / n));
        lines.push(samples);
      }

      for (const samples of lines) emit(samples, false);   // whole weave, complete
      paintFaces();                                        // the solid covers it
      for (const samples of lines) emit(samples, true);    // near stretches restored
    }
  }
  return Component;
};
