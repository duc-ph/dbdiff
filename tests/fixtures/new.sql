SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS=0;

DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id` int(11) NOT NULL,
  `name` varchar(100) NOT NULL,
  `email` varchar(200) DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  `phone` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `users` VALUES
(1,'Alice','alice@ex.com','2024-02-01 00:00:00',NULL),
(2,'Bob','bob@ENG.com','2024-02-01 00:00:00',NULL),
(3,'Carol','carol@ex.com','2024-01-01 00:00:00','555-0003'),
(5,'Eve','eve@ex.com','2024-02-01 00:00:00','555-1234');

DROP TABLE IF EXISTS `orders`;
CREATE TABLE `orders` (
  `order_id` int(11) NOT NULL,
  `line_no` int(11) NOT NULL,
  `sku` varchar(32) NOT NULL,
  `qty` int(11) NOT NULL,
  PRIMARY KEY (`order_id`,`line_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `orders` VALUES
(100,1,'A',5),
(100,2,'B',9),
(101,1,'C',1),
(102,1,'D',7);

DROP TABLE IF EXISTS `tags`;
CREATE TABLE `tags` (
  `name` varchar(50) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `tags` VALUES ('red'),('green');

DROP TABLE IF EXISTS `sessions`;
CREATE TABLE `sessions` (
  `id` int(11) NOT NULL,
  `token` varchar(64) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `sessions` VALUES (1,'abc'),(2,'def');
