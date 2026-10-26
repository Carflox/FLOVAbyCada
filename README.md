# FLOVA by Cada — catálogo, cuentas de clientes y pedidos

Proyecto responsive en español con catálogo de productos y servicios, carrito, métodos de pago preferidos, acceso de clientes separado del administrador, verificación de correo por código, historial y seguimiento de pedidos. Backend Flask + SQLite; interfaz HTML/CSS/JavaScript.

## Servicios por WhatsApp
Los servicios no se agregan al carrito: cada opción (portabilidad, línea nueva, plan WiFi, servicio técnico o compra y venta de criptos) abre WhatsApp con un mensaje que identifica el servicio solicitado. Los productos se agregan al carrito y se pagan en el checkout.

## Contacto y redes sociales
La página principal muestra los canales públicos que configures en el servidor: `FLOVA_CONTACT_WHATSAPP` (número internacional solo con dígitos), `FLOVA_CONTACT_EMAIL` y las URL HTTPS `FLOVA_INSTAGRAM_URL`, `FLOVA_FACEBOOK_URL` y `FLOVA_TIKTOK_URL`. Los puedes agregar a `.env` en local o como variables del servicio en Render. No publiques secretos en GitHub; `.env.example` contiene valores de ejemplo sin secretos y se puede subir.

## Requisitos e instalación local
1. Instala Python 3.10 o superior (Python 3.13 es compatible).
2. Descomprime el ZIP y abre la carpeta `flova_by_cada_web` completa en VS Code.
3. En Terminal → Nueva terminal ejecuta:
   ```powershell
   python -m pip install -r requirements.txt
   python app.py
   ```
4. Abre `http://127.0.0.1:5000`. Panel de administración: `http://127.0.0.1:5000/admin`. Cuenta de cliente: `http://127.0.0.1:5000/cuenta`.

## Administración
Credenciales iniciales para pruebas: usuario `admin`, contraseña `cambia-esta-clave`. Antes de publicar, configura valores propios en las variables `FLOVA_ADMIN_USER`, `FLOVA_ADMIN_PASSWORD` y `FLOVA_SECRET_KEY`.

## Cuentas de clientes
- Registro con nombre completo, correo, documento, celular y contraseña.
- Verificación del correo mediante código de seis dígitos válido durante 10 minutos.
- El registro se confirma con un código enviado al correo; al iniciar sesión, el código se vuelve a pedir si han pasado 30 días desde la última verificación.
- Correo de bienvenida al completar el registro y aviso al confirmar el inicio de sesión.
- Panel personal inspirado en la distribución de las capturas compartidas: menú lateral de datos personales, direcciones, medios de pago, compras y seguridad, adaptado a los colores y la marca FLOVA. Permite editar perfil, guardar direcciones y consultar pedidos y pagos.
- Solo se muestran los pedidos asociados al cliente que inició sesión.
- Para poder hacer seguimiento desde la cuenta, el cliente debe iniciar sesión antes de enviar el pedido.
- Al aprobarse un pago y cuando el administrador cambia el estado del pedido, se envía un correo al cliente.

## Pagos en línea
El carrito conserva sus productos al ir a Mi cuenta. El checkout /checkout sigue Carrito → Entrega → Pago y presenta Tarjeta, Yape y PagoEfectivo dentro de la tienda. Las tarjetas usan Card Payment Brick; Yape usa token OTP de Mercado Pago; PagoEfectivo genera un CIP y deja el pedido pendiente hasta recibir confirmación. /pago/resultado muestra estado, resumen, instrucciones y etapas de entrega. El stock se reserva al iniciar el pago y se libera si Mercado Pago rechaza o cancela la orden.

El formulario permite seleccionar o guardar una dirección de entrega. El costo y la fecha de envío se coordinan con el cliente y no se agregan al cobro. La disponibilidad de cada método depende de la cuenta y de los métodos que Mercado Pago tenga habilitados.



Para activar pagos reales, crea una aplicación en Mercado Pago y configura estas variables en el servidor:

- FLOVA_MP_PUBLIC_KEY: Public Key de la aplicación para iniciar MercadoPago.js y el formulario seguro de tarjeta/Yape. Es pública y no reemplaza el Access Token.
- FLOVA_MP_ACCESS_TOKEN: Access Token privado (usa credenciales de prueba primero).
- `FLOVA_MP_CLIENT_ID` y `FLOVA_MP_CLIENT_SECRET`: credenciales privadas de la aplicación; si completas ambas, FLOVA obtiene y renueva automáticamente un token con OAuth `client_credentials` para operar en nombre de la propia tienda. También puedes mantener el Access Token fijo anterior.
- `FLOVA_MP_CALLER_ID`: ID numérico del vendedor propietario de la aplicación, requerido para enviar evidencia de contracargos.
- `FLOVA_PUBLIC_URL`: URL pública HTTPS del sitio, sin `/` al final.
- `FLOVA_MP_WEBHOOK_SECRET`: clave secreta del evento **Order (Mercado Pago)** en el panel de Webhooks.

Configura en Mercado Pago el webhook `https://TU-DOMINIO/api/payments/mercadopago/webhook` con el evento **Order (Mercado Pago)**. El servidor verifica la firma del webhook y consulta la order con el Access Token antes de confirmar el pago. No compartas ni subas las credenciales al repositorio. Para confirmar cambios de estado en producción, el webhook debe apuntar a la URL HTTPS pública de FLOVA.

En el panel `/admin`, la sección **Pedidos recibidos** permite consultar órdenes de Mercado Pago por rango de fechas, referencia externa y estado, con paginación. La consulta solo muestra datos operativos y omite tokens del cliente y enlaces de validación 3DS. Los pedidos con pago pendiente incluyen **Cancelar pago**: consulta primero el estado remoto, cancela solo órdenes en estado `created` o `action_required`, usa una clave de idempotencia y sincroniza el pedido y el stock cuando Mercado Pago confirma la cancelación. Los pagos confirmados incluyen **Reembolsar** para devolución total o parcial, con validación del monto disponible y actualización del estado local.

También puedes consultar un contracargo desde **Pedidos recibidos → Consultar contracargo** usando su ID de Mercado Pago. El panel muestra importe, motivo, estado de documentación, fechas, cobertura y pagos relacionados. Los documentos se abren a través del endpoint oficial `GET /v1/chargebacks/documentation/{type}/{uuid}`, con el Access Token y `FLOVA_MP_CALLER_ID` en el servidor; el navegador recibe el archivo con su MIME original sin exponer credenciales. Si el estado de documentación está pendiente, puedes adjuntar hasta cinco archivos PDF, JPG o PNG (máximo 5 MB en conjunto). Antes del envío, FLOVA verifica de nuevo el estado pendiente; Mercado Pago acepta evidencia una sola vez por caso.

En Render agrega FLOVA_MP_PUBLIC_KEY junto con el Access Token privado u OAuth; en desarrollo puedes configurarlas en .env. Conserva Client Secret y Access Token solo en el servidor. Con OAuth, el token temporal se mantiene en memoria y se renueva al vencer; nunca se expone al navegador. .env está excluido de Git.

El servidor ya usa la base URL `https://api.mercadopago.com` y agrega automáticamente `Authorization: Bearer` con el token generado por OAuth o con el Access Token fijo. Mantén el Client Secret y los tokens únicamente en el servidor; nunca los pegues en el HTML, JavaScript del navegador, capturas públicas ni repositorios. Para comenzar, utiliza las credenciales de prueba de la aplicación de Mercado Pago.

## Configurar el envío real de correos (SMTP)
Si no configuras SMTP, el proyecto funciona en modo local de prueba: los correos y códigos se imprimen en la terminal de VS Code y no llegan realmente a la bandeja del cliente. Para producción, configura las siguientes variables de entorno antes de iniciar Flask:

- `FLOVA_SMTP_HOST`: servidor SMTP de tu proveedor de correo.
- `FLOVA_SMTP_PORT`: normalmente `587` para STARTTLS o `465` con SSL.
- `FLOVA_SMTP_USER`: cuenta autenticada que enviará los correos.
- `FLOVA_SMTP_PASSWORD`: contraseña de aplicación o credencial SMTP del proveedor.
- `FLOVA_FROM_EMAIL`: dirección que aparecerá como remitente.
- Opcional: `FLOVA_SMTP_SSL=1` para conexión SSL directa en puerto 465.

Ejemplo de PowerShell (reemplaza los valores con tus credenciales reales; no los compartas en chats ni los subas a Git):
```powershell
$env:FLOVA_ADMIN_USER="tu_usuario_admin"
$env:FLOVA_ADMIN_PASSWORD="una_clave_larga_y_unica"
$env:FLOVA_SECRET_KEY="una_clave_secreta_larga_y_aleatoria"
$env:FLOVA_SMTP_HOST="smtp.tu-proveedor.com"
$env:FLOVA_SMTP_PORT="587"
$env:FLOVA_SMTP_USER="notificaciones@tudominio.com"
$env:FLOVA_SMTP_PASSWORD="tu_credencial_smtp_o_contrasena_de_aplicacion"
$env:FLOVA_FROM_EMAIL="FLOVA by Cada <notificaciones@tudominio.com>"
python app.py
```
Si Gmail no permite crear una contraseña de aplicación, puedes usar cualquier proveedor transaccional que entregue credenciales SMTP. En su panel busca **SMTP**, **SMTP relay** o **SMTP credentials** y copia el host, puerto, usuario y contraseña SMTP en las variables anteriores. Usa como `FLOVA_FROM_EMAIL` un remitente autorizado por ese proveedor. No uses tu contraseña personal de correo. En Render, agrega cada variable en **Environment Variables**, guarda y espera el nuevo despliegue. Haz una prueba de registro y revisa los logs de Render; un correo solo se considera enviado si el proveedor acepta la conexión y el mensaje.

## Funciones de tienda
- Catálogos independientes de productos y servicios con imágenes ampliables.
- Categorías, búsqueda, filtros de precio y disponibilidad.
- Stock: Disponible, Última unidad y Agotado.
- Carrito con cantidad y total estimado.
- Pagos en línea integrados en FLOVA con Mercado Pago Orders: formulario seguro de tarjeta, Yape y PagoEfectivo; consulta la sección **Pagos en línea** para configurar credenciales y webhooks.
- El panel administrativo permite crear, editar y eliminar artículos y actualizar estados de pedidos.

## Base de datos y actualización
SQLite crea `flova.db` automáticamente. El inicio aplica migraciones seguras para añadir campos nuevos a instalaciones previas. Antes de actualizar un proyecto que ya contiene productos, copia `flova.db` y la carpeta `static/uploads` al proyecto nuevo.

### Base de datos persistente en Render (sin costo inicial)
Render Free elimina los archivos locales cuando el servicio se reinicia, se despliega o vuelve a arrancar después de inactividad. Por eso SQLite local (`flova.db`) no debe usarse allí para cuentas y pedidos que deban conservarse. FLOVA usa PostgreSQL cuando existe la variable `DATABASE_URL`; sin ella, mantiene SQLite para desarrollo local.

Para comenzar con una base PostgreSQL gratuita, crea un proyecto en Supabase y en **Connect** copia la cadena de **Session pooler** (Render requiere una conexión IPv4). En Render → servicio FLOVA → **Environment**, agrega `DATABASE_URL` con esa cadena y guarda para que se despliegue la aplicación. No la publiques ni la compartas. En la primera conexión, FLOVA crea sus tablas automáticamente. Las cuentas y pedidos que sigan únicamente en el viejo `flova.db` no se copian automáticamente; expórtalos antes del cambio si están disponibles. El plan gratuito de Supabase limita el almacenamiento y puede pausar proyectos con poca actividad, así que reactívalo desde su panel si ocurre.

La base Render Free tiene su propio límite: sus bases PostgreSQL gratuitas vencen a los 30 días. No la uses para guardar datos de clientes a largo plazo sin tener un plan de respaldo/migración.

## Antes de publicar
- Usa HTTPS y un servidor WSGI de producción; desactiva el modo debug (`$env:FLOVA_DEBUG="0"`).
- Cambia todas las credenciales de ejemplo.
- Configura SMTP y prueba que los mensajes realmente llegan.
- Haz copias de seguridad de la base de datos y las imágenes.
- No recopiles información de pago sensible en formularios propios; esta versión solo registra el método de pago preferido y no almacena números de tarjetas.

## Estructura
```text
app.py
requirements.txt
schema.sql
templates/index.html
templates/admin.html
templates/login.html
templates/account.html
templates/checkout.html
static/css/style.css
static/js/store.js
static/js/admin.js
static/uploads/
```


### Acceso Mi cuenta en el encabezado

La tienda consulta la sesión del cliente al cargar. Si el cliente ha iniciado sesión o completado el registro y verificación, el botón del encabezado cambia a **Mi cuenta** y abre su panel privado en `/cuenta`. Si no hay sesión activa, se muestra **Iniciar sesión / Registrarse**. El acceso de administración permanece separado en `/admin`.


