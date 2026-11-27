// Photo preparation and uploads with real progress for the chat and the inbox.
const LONG_SIDE = 3200;
const HEAVY = 1.5 * 1024 * 1024;
const REDUCIBLE = new Set(['image/jpeg', 'image/png', 'image/webp']);

export const ACCEPTED_FILES = /\.(jpe?g|png|webp|heic|heif|pdf)$/i;
export const ACCEPTED_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif', 'application/pdf']);

async function decode(file) {
  if (typeof createImageBitmap === 'function') {
    try {
      // 'from-image' applies the EXIF orientation so the canvas receives the upright photo.
      return await createImageBitmap(file, {imageOrientation: 'from-image'});
    } catch { /* Fall back to an image element, which also follows EXIF orientation. */ }
  }
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.decoding = 'async';
    image.src = url;
    await image.decode();
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function dimensions(picture) {
  return [picture.naturalWidth || picture.width, picture.naturalHeight || picture.height];
}

// Reduce heavy or very large photos to JPEG before sending them. PDF, HEIC and anything the
// browser cannot decode are sent untouched; the server converts HEIC by itself.
export async function prepareImage(file) {
  if (!REDUCIBLE.has(file.type) && !/\.(jpe?g|png|webp)$/i.test(file.name)) return file;
  let picture;
  try {
    picture = await decode(file);
    const [width, height] = dimensions(picture);
    if (!width || !height) return file;
    const longest = Math.max(width, height);
    if (file.size <= HEAVY && longest <= LONG_SIDE) return file;
    const scale = Math.min(1, LONG_SIDE / longest);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const context = canvas.getContext('2d');
    context.fillStyle = '#fff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(picture, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
    if (!blob || (longest <= LONG_SIDE && blob.size >= file.size)) return file;
    const name = file.name.replace(/\.[^.]*$/, '') + '.jpg';
    return new File([blob], name, {type: 'image/jpeg', lastModified: file.lastModified});
  } catch {
    return file;
  } finally {
    picture?.close?.();
  }
}

export function prepareFiles(files) {
  return Promise.all([...files].map(prepareImage));
}

function failure(xhr) {
  const type = xhr.getResponseHeader('content-type') || '';
  if (type.includes('application/json')) {
    try {
      const data = JSON.parse(xhr.responseText);
      return typeof data.detail === 'string' ? data.detail : 'Revisa los campos capturados.';
    } catch { /* Use the text below. */ }
  }
  return (xhr.responseText || '').slice(0, 450) || 'No se completó el envío.';
}

// POST a form with XMLHttpRequest so the panel can show the real upload progress (0 to 1).
export function sendForm(path, body, {token, onProgress = () => {}} = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.setRequestHeader('x-app-token', token || '');
    xhr.responseType = 'text';
    xhr.upload.onprogress = event => {
      if (event.lengthComputable && event.total) onProgress(Math.min(1, event.loaded / event.total));
    };
    xhr.upload.onload = () => onProgress(1);
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) return reject(Error(failure(xhr)));
      try { resolve(JSON.parse(xhr.responseText)); }
      catch { reject(Error('La respuesta del panel no es válida.')); }
    };
    xhr.onerror = () => reject(Error('Se perdió la conexión durante el envío. Puedes volver a intentarlo.'));
    xhr.onabort = () => reject(Error('Envío cancelado.'));
    onProgress(0);
    xhr.send(body);
  });
}

export function sizeLabel(bytes) {
  return bytes >= 1024 * 1024 ? (bytes / 1024 / 1024).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
}
