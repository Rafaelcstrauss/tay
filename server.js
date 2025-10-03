const express = require('express');
const http = require('http');
const { SerialPort } = require('serialport');
const { ReadlineParser } = require('@serialport/parser-readline');
const socketIo = require('socket.io');
const app = express();
const server = http.createServer(app);
const io = socketIo(server);
const PORT = 3000;
// Ajuste a porta serial conforme seu sistema (ex: COM3 no Windows, /dev/ttyUSB0 no Linux)
const SERIAL_PORT = '/dev/ttyUSB0'; 
const BAUD_RATE = 9600;
// Configura a porta serial
const port = new SerialPort({
  path: SERIAL_PORT,
  baudRate: BAUD_RATE,
});
const parser = port.pipe(new ReadlineParser({ delimiter: '\n' }));
// Serve arquivos estáticos (ex: index.html)
app.use(express.static('public'));
// Quando receber dados do Arduino, envia para os clientes via websocket
parser.on('data', (line) => {
  console.log('Arduino:', line.trim());
  io.emit('arduino-data', line.trim());
});
io.on('connection', (socket) => {
  console.log('Cliente conectado');
});
server.listen(PORT, () => {
  console.log(`Servidor rodando em http://localhost:${PORT}`);
});
