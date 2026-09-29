import * as THREE from 'three';
import { homeMotion, visualTest } from './motion';

export type GlobeScene = ReturnType<typeof createGlobeScene>;
export function createGlobeScene(host: HTMLElement, mask: ImageData, low: boolean, onFailure: () => void) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, low ? 1 : 1.5));
  renderer.setClearColor(0x080b0d, 0);
  renderer.domElement.setAttribute('aria-hidden', 'true');
  host.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1.5, 1.5, 1.1, -1.1, .1, 10);
  camera.position.z = 4;
  const globe = new THREE.Group();
  globe.rotation.set(.18, -.36, -.13);
  scene.add(globe);
  const geometries = new Set<THREE.BufferGeometry>();
  const materials = new Set<THREE.Material>();
  function track<T extends THREE.Mesh | THREE.Points | THREE.Line>(object: T) {
    geometries.add(object.geometry);
    for (const material of [object.material].flat()) materials.add(material);
    globe.add(object);
    return object;
  }
  const sphereVertex = `varying vec3 vNormal; varying vec3 vPosition;
    void main(){ vec4 p=modelViewMatrix*vec4(position,1.0); vPosition=p.xyz;
    vNormal=normalize(normalMatrix*normal); gl_Position=projectionMatrix*p; }`;
  track(new THREE.Mesh(new THREE.SphereGeometry(.997, 96, 64), new THREE.ShaderMaterial({
    vertexShader: sphereVertex,
    fragmentShader: `varying vec3 vNormal; varying vec3 vPosition;
      void main(){vec3 n=normalize(vNormal); vec3 v=normalize(-vPosition);
      float light=max(dot(n,normalize(vec3(-.7,.85,1.0))),0.0);
      float rim=pow(1.0-max(dot(n,v),0.0),8.0);
      vec3 color=vec3(.022,.03,.036)+vec3(.02,.027,.032)*light;
      color+=vec3(.29,.34,.38)*rim*pow(light,.8);
      gl_FragColor=vec4(color,1.0); }`,
  })));
  track(new THREE.Mesh(new THREE.SphereGeometry(1.006, 96, 64), new THREE.ShaderMaterial({
    vertexShader: sphereVertex, transparent: true, depthWrite: false,
    fragmentShader: `varying vec3 vNormal; varying vec3 vPosition;
      void main(){vec3 n=normalize(vNormal); float rim=pow(1.0-max(dot(n,normalize(-vPosition)),0.0),7.0);
      float light=max(dot(n,normalize(vec3(-.8,.8,.4))),0.0);
      gl_FragColor=vec4(.78,.84,.89,rim*(.08+.45*light));}`,
  })));
  const positions: number[] = [], brightness: number[] = [];
  let seed = 20260928;
  function random() { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; }
  const samples = low ? 43000 : 82000;
  for (let i = 0; i < samples; i++) {
    const y = 1 - 2 * (i + .5) / samples;
    const phi = i * Math.PI * (3 - Math.sqrt(5));
    const r = Math.sqrt(1 - y * y), x = Math.cos(phi) * r, z = Math.sin(phi) * r;
    const lon = Math.atan2(x, z), lat = Math.asin(y);
    const u = Math.min(mask.width - 1, Math.floor((lon / (2 * Math.PI) + .5) * mask.width));
    const v = Math.min(mask.height - 1, Math.floor((.5 - lat / Math.PI) * mask.height));
    if (mask.data[(v * mask.width + u) * 4] < 128) continue;
    positions.push(x * 1.001, y * 1.001, z * 1.001);
    brightness.push(.5 + random() * .5);
  }
  // Keep a lower draw range spread across the whole globe, not just northern latitudes.
  for (let i = brightness.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [brightness[i], brightness[j]] = [brightness[j], brightness[i]];
    for (let axis = 0; axis < 3; axis++) [positions[i * 3 + axis], positions[j * 3 + axis]] = [positions[j * 3 + axis], positions[i * 3 + axis]];
  }
  const landGeometry = new THREE.BufferGeometry();
  landGeometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  landGeometry.setAttribute('brightness', new THREE.Float32BufferAttribute(brightness, 1));
  const pointMaterial = new THREE.ShaderMaterial({
    uniforms: { dpr: { value: renderer.getPixelRatio() } }, transparent: true, depthWrite: false,
    vertexShader: `attribute float brightness; uniform float dpr; varying float alpha;
      void main(){vec3 n=normalize(normalMatrix*normalize(position));
      float light=max(dot(n,normalize(vec3(-.6,.9,.8))),0.0);
      alpha=brightness*(.22+.88*light); vec4 p=modelViewMatrix*vec4(position,1.0);
      gl_Position=projectionMatrix*p; gl_PointSize=(1.1+.45*brightness)*dpr;}`,
    fragmentShader: `varying float alpha; void main(){float r=length(gl_PointCoord-.5);
      if(r>.5) discard; gl_FragColor=vec4(.79,.85,.9,alpha*(1.0-smoothstep(.18,.5,r)));}`,
  });
  const land = track(new THREE.Points(landGeometry, pointMaterial));
  function coordinate(lon: number, lat: number) {
    const p = lat * Math.PI / 180, t = lon * Math.PI / 180;
    return new THREE.Vector3(Math.cos(p) * Math.sin(t), Math.sin(p), Math.cos(p) * Math.cos(t));
  }
  // Decorative routes; these never encode a wallet location or a transfer.
  const routes = [[-72, 40, 18, 51], [18, 51, 105, 3], [-44, -20, 105, 3], [-72, 40, 36, -5], [18, 51, 36, -5]];
  const arcPositions: number[] = [], arcColors: number[] = [], endpoints: number[] = [];
  const curves: THREE.Vector3[][] = [];
  for (const [index, route] of routes.entries()) {
    const a = coordinate(route[0], route[1]), b = coordinate(route[2], route[3]);
    const angle = a.angleTo(b), curve: THREE.Vector3[] = [];
    for (let step = 0; step <= 72; step++) {
      const t = step / 72;
      curve.push(a.clone().multiplyScalar(Math.sin((1 - t) * angle) / Math.sin(angle))
        .addScaledVector(b, Math.sin(t * angle) / Math.sin(angle))
        .multiplyScalar(1.008 + (.12 + index * .015) * Math.sin(Math.PI * t)));
    }
    for (let step = 0; step < 72; step++) {
      arcPositions.push(...curve[step].toArray(), ...curve[step + 1].toArray());
      const c = index === 2 ? .65 : .36;
      arcColors.push(c, c * 1.03, c * 1.08, c, c * 1.03, c * 1.08);
    }
    endpoints.push(...a.multiplyScalar(1.01).toArray(), ...b.multiplyScalar(1.01).toArray());
    curves.push(curve);
  }
  const arcGeometry = new THREE.BufferGeometry();
  arcGeometry.setAttribute('position', new THREE.Float32BufferAttribute(arcPositions, 3));
  arcGeometry.setAttribute('color', new THREE.Float32BufferAttribute(arcColors, 3));
  track(new THREE.LineSegments(arcGeometry, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: .73 })));
  for (const tilt of [-.45, .8]) {
    const points = Array.from({ length: 161 }, (_, i) => {
      const t = i / 160 * Math.PI * 2;
      return new THREE.Vector3(1.24 * Math.cos(t), 1.04 * Math.sin(t), 0).applyAxisAngle(new THREE.Vector3(0, 1, 0), tilt).applyAxisAngle(new THREE.Vector3(0, 0, 1), tilt * .5);
    });
    track(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color: 0x87959e, transparent: true, opacity: .17 })));
  }
  function glow(geometry: THREE.BufferGeometry, size: number) {
    return track(new THREE.Points(geometry, new THREE.ShaderMaterial({
      uniforms: { size: { value: size * renderer.getPixelRatio() } }, transparent: true, depthWrite: false,
      vertexShader: `uniform float size; void main(){gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);gl_PointSize=size;}`,
      fragmentShader: `void main(){float r=length(gl_PointCoord-.5); if(r>.5)discard;
        float a=exp(-r*8.0);gl_FragColor=vec4(.89,.95,1.0,a);}`,
    })));
  }
  glow(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(endpoints, 3)), 12);
  const travellers = glow(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(6), 3)), 18);
  let time = 0, running = false, disposed = false, raf = 0, previous = 0, frames = 0;
  let slowFrames = 0, downgraded = low;
  const target = new THREE.Vector2(), tilt = new THREE.Vector2();
  function render() {
    globe.rotation.y = -.36 + time * homeMotion.rotation + tilt.x;
    globe.rotation.x = .18 + tilt.y;
    const attribute = travellers.geometry.getAttribute('position');
    for (let i = 0; i < 2; i++) {
      const curve = curves[i === 0 ? 2 : 0];
      const at = ((time / homeMotion.travel + i * .48) % 1) * 72;
      const point = curve[Math.floor(at)].clone().lerp(curve[Math.min(72, Math.floor(at) + 1)], at % 1);
      attribute.setXYZ(i, point.x, point.y, point.z);
    }
    attribute.needsUpdate = true;
    renderer.render(scene, camera);
    frames++;
    host.dataset.frames = String(frames);
  }
  function frame(now: number) {
    if (!running || disposed) return;
    const elapsed = previous ? (now - previous) / 1000 : 0;
    previous = now;
    time += Math.min(elapsed, .05);
    tilt.lerp(target, 1 - Math.exp(-Math.min(elapsed, .05) * 3));
    if (elapsed > .035) slowFrames++; else slowFrames = Math.max(0, slowFrames - 1);
    if (slowFrames > 100) {
      if (downgraded) { setRunning(false); onFailure(); return; }
      downgraded = true; slowFrames = 0;
      renderer.setPixelRatio(1); pointMaterial.uniforms.dpr.value = 1;
      land.geometry.setDrawRange(0, Math.floor(brightness.length * .6));
      host.dataset.quality = 'reduced';
    }
    render();
    raf = requestAnimationFrame(frame);
  }
  function setRunning(value: boolean) {
    value = value && !visualTest && !disposed;
    if (value === running) return;
    running = value; host.dataset.running = String(value);
    cancelAnimationFrame(raf); previous = 0;
    if (value) raf = requestAnimationFrame(frame);
  }
  function resize() {
    if (disposed) return;
    const width = host.clientWidth, height = host.clientHeight;
    if (!width || !height) return;
    const half = 1.09, aspect = width / height;
    camera.left = -half * aspect; camera.right = half * aspect;
    camera.top = half; camera.bottom = -half; camera.updateProjectionMatrix();
    renderer.setSize(width, height); render();
  }
  const observer = new ResizeObserver(resize); observer.observe(host);
  function move(event: PointerEvent) {
    if (event.pointerType !== 'mouse' || !running) return;
    const rect = host.getBoundingClientRect();
    target.set(((event.clientX - rect.left) / rect.width - .5) * 2 * homeMotion.tilt, ((event.clientY - rect.top) / rect.height - .5) * homeMotion.tilt);
  }
  const leave = () => target.set(0, 0);
  const stage = host.parentElement!;
  stage.addEventListener('pointermove', move); stage.addEventListener('pointerleave', leave);
  function lost(event: Event) { event.preventDefault(); setRunning(false); onFailure(); }
  renderer.domElement.addEventListener('webglcontextlost', lost);
  host.dataset.running = 'false'; host.dataset.quality = low ? 'reduced' : 'full';
  resize();
  const controller = {
    setRunning,
    snapshot: () => { render(); return renderer.domElement.toDataURL('image/webp', .92); },
    stats: () => ({ frames, running, time, points: brightness.length, drawCalls: renderer.info.render.calls, dpr: renderer.getPixelRatio(), quality: host.dataset.quality }),
    dispose: () => {
      if (disposed) return;
      setRunning(false); disposed = true; observer.disconnect();
      stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerleave', leave);
      renderer.domElement.removeEventListener('webglcontextlost', lost);
      geometries.forEach(g => g.dispose()); materials.forEach(m => m.dispose());
      renderer.dispose(); renderer.forceContextLoss(); renderer.domElement.remove();
      if (import.meta.env.DEV && window.__SMART_MONEY_GLOBE__ === controller) delete window.__SMART_MONEY_GLOBE__;
    },
  };
  if (import.meta.env.DEV) window.__SMART_MONEY_GLOBE__ = controller;
  return controller;
}

declare global { interface Window { __SMART_MONEY_GLOBE__?: GlobeScene } }
